export const PLATFORM_GRID_STYLES = `
      :host {
        display: block;
      }

      /* ── Grid container ── */
      .grid {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        overflow: hidden;
      }

      /* ── Toolbar ── */
      .toolbar {
        display: flex;
        align-items: center;
        gap: var(--bk-gap-sm);
        padding: 10px 14px;
        border-bottom: 1px solid var(--border);
        flex-wrap: wrap;
      }
      .search {
        position: relative;
        flex: 1;
        min-width: 180px;
      }
      .search-icon {
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        width: 14px;
        height: 14px;
        color: var(--text-3);
        pointer-events: none;
      }
      .search input {
        width: 100%;
        padding: 6px 10px 6px 30px;
        font-size: var(--bk-font-sm);
        font-family: inherit;
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        background: var(--bg);
        color: var(--text-1);
        outline: none;
        transition: border-color 0.12s;
      }
      .search input:focus {
        border-color: var(--accent);
      }
      .search input::placeholder {
        color: var(--text-3);
      }
      .toolbar-actions {
        display: flex;
        gap: var(--bk-gap-sm);
        flex-shrink: 0;
      }

      /* ── Bulk bar ── */
      .bulk-bar {
        display: flex;
        align-items: center;
        gap: var(--bk-gap-sm);
        padding: 8px 14px;
        background: var(--accent-soft);
        border-bottom: 1px solid var(--accent-border);
        font-size: var(--bk-font-sm);
        color: var(--accent);
        flex-wrap: wrap;
      }
      .bulk-count {
        font-weight: 600;
        margin-right: var(--bk-gap-sm);
      }
      .bulk-actions {
        display: flex;
        gap: var(--bk-gap-sm);
      }

      /* ── Filter bar ── */
      .filter-bar {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--bk-gap-sm);
        padding: 8px 14px;
        border-bottom: 1px solid var(--border);
      }
      .filter-field {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: var(--bk-font-xs);
      }
      .filter-label {
        color: var(--text-3);
        font-weight: 500;
        white-space: nowrap;
      }
      .filter-field select,
      .filter-field input {
        padding: 3px 8px;
        font-size: var(--bk-font-xs);
        font-family: inherit;
        border: 1px solid var(--border);
        border-radius: var(--r-sm);
        background: var(--bg);
        color: var(--text-1);
        outline: none;
        min-width: 80px;
      }
      .filter-field select:focus,
      .filter-field input:focus {
        border-color: var(--accent);
      }

      /* ── Table ── */
      .table-wrap {
        overflow-x: auto;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--bk-font-sm);
      }
      thead th {
        padding: 8px 12px;
        text-align: left;
        font-size: var(--bk-font-xs);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--text-3);
        border-bottom: 1px solid var(--border);
        white-space: nowrap;
        user-select: none;
        background: var(--bg);
      }
      thead th.sortable {
        cursor: pointer;
      }
      thead th.sortable:hover {
        background: var(--hover);
        color: var(--text-1);
      }
      .sort-indicator {
        display: inline-block;
        margin-left: 4px;
        font-size: 10px;
        vertical-align: middle;
        opacity: 0.5;
      }
      .sort-indicator.active {
        opacity: 1;
        color: var(--accent);
      }
      tbody td {
        padding: 8px 12px;
        border-bottom: 1px solid var(--border);
        color: var(--text-1);
        vertical-align: middle;
      }
      tbody tr:hover td {
        background: var(--hover);
      }
      tbody tr:last-child td {
        border-bottom: none;
      }

      /* ── Cell types ── */
      .cell-mono {
        font-family: var(--bk-mono);
        font-size: var(--bk-font-xs);
      }
      .cell-truncate {
        display: block;
        width: 100%;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
        max-width: var(--bk-cell-truncate-width, 28rem);
      }
      .cell-number {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .cell-link {
        color: var(--accent);
        text-decoration: none;
        cursor: pointer;
      }
      .cell-link:hover {
        text-decoration: underline;
      }
      .cell-id {
        font-family: var(--bk-mono);
        font-size: var(--bk-font-xs);
        color: var(--text-3);
      }

      /* ── Badge ── */
      .badge {
        display: inline-flex;
        padding: 2px 8px;
        font-size: var(--bk-font-xs);
        font-weight: 500;
        border-radius: 999px;
        white-space: nowrap;
        line-height: 1.5;
      }
      .badge-green  { background: var(--green-soft);  color: var(--green);  border: 1px solid var(--green-border); }
      .badge-red    { background: var(--red-soft);    color: var(--red);    border: 1px solid var(--red-border); }
      .badge-amber  { background: var(--amber-soft);  color: var(--amber);  border: 1px solid var(--amber-border); }
      .badge-accent { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-border); }
      .badge-neutral{ background: var(--neutral-soft);color: var(--neutral);border: 1px solid var(--neutral-border); }

      /* ── Checkbox ── */
      .row-checkbox {
        accent-color: var(--accent);
        width: 14px;
        height: 14px;
        cursor: pointer;
      }

      /* ── Row actions ── */
      .row-actions {
        display: flex;
        gap: 4px;
        white-space: nowrap;
      }
      .row-actions-sticky {
        position: sticky;
        right: 0;
        z-index: 2;
        background: var(--surface, var(--fs-table-card-bg, #18181f));
        box-shadow: -10px 0 14px -14px rgb(0 0 0 / 55%);
      }
      tbody td.row-actions-sticky,
      tbody tr:hover td.row-actions-sticky,
      tbody tr:focus-within td.row-actions-sticky {
        background: var(--surface, var(--fs-table-card-bg, #18181f));
        border-left: 1px solid var(--border, var(--fs-table-card-border, rgb(255 255 255 / 10%)));
      }
      thead .row-actions-sticky {
        z-index: 3;
      }

      /* ── Shared button ── */
      .bk-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 7px 14px;
        font-size: var(--bk-font-sm);
        font-weight: 500;
        font-family: inherit;
        border-radius: var(--r-md);
        border: 1px solid var(--border);
        background: var(--surface);
        color: var(--text-1);
        cursor: pointer;
        transition: background 0.12s, border-color 0.12s;
        user-select: none;
        line-height: 1.4;
      }
      .bk-btn:hover {
        background: var(--hover);
      }
      .bk-btn.primary {
        background: var(--accent);
        color: #fff;
        border-color: var(--accent);
      }
      .bk-btn.primary:hover {
        filter: brightness(1.1);
      }
      .bk-btn.danger {
        background: var(--red);
        color: #fff;
        border-color: var(--red);
      }
      .bk-btn.danger:hover {
        filter: brightness(1.1);
      }
      .bk-btn.ghost {
        background: transparent;
        border-color: transparent;
        color: var(--text-2);
      }
      .bk-btn.ghost:hover {
        background: var(--hover);
        color: var(--text-1);
      }
      .bk-btn.sm {
        padding: 4px 10px;
        font-size: var(--bk-font-xs);
      }
      .bk-btn.lg {
        padding: 10px 20px;
        font-size: var(--bk-font-base);
      }
      .btn-icon {
        width: 14px;
        height: 14px;
        flex-shrink: 0;
      }

      /* ── Footer / Pagination ── */
      .footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 14px;
        border-top: 1px solid var(--border);
        font-size: var(--bk-font-xs);
        color: var(--text-3);
        flex-wrap: wrap;
        gap: var(--bk-gap-sm);
      }
      .footer-left {
        display: flex;
        align-items: center;
        gap: var(--bk-gap-sm);
      }
      .footer-right {
        display: flex;
        align-items: center;
        gap: var(--bk-gap-sm);
      }
      .page-size-select {
        padding: 3px 6px;
        font-size: var(--bk-font-xs);
        font-family: inherit;
        border: 1px solid var(--border);
        border-radius: var(--r-sm);
        background: var(--bg);
        color: var(--text-1);
        outline: none;
      }
      .page-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        border: 1px solid var(--border);
        border-radius: var(--r-sm);
        background: var(--surface);
        color: var(--text-2);
        cursor: pointer;
        font-size: var(--bk-font-sm);
        transition: background 0.12s;
      }
      .page-btn:hover:not(:disabled) {
        background: var(--hover);
        color: var(--text-1);
      }
      .page-btn:disabled {
        opacity: 0.35;
        cursor: default;
      }
      .page-info {
        padding: 0 4px;
      }

      /* ── Empty / Loading / Error ── */
      .empty-state {
        padding: 48px 20px;
        text-align: center;
      }
      .empty-title {
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-2);
        margin: 0 0 4px;
      }
      .empty-desc {
        font-size: var(--bk-font-sm);
        color: var(--text-3);
        margin: 0;
      }
      .loading-overlay {
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 48px 20px;
        color: var(--text-3);
        font-size: var(--bk-font-sm);
      }
      .error-bar {
        padding: 10px 14px;
        background: var(--red-soft);
        color: var(--red);
        border-bottom: 1px solid var(--red-border);
        font-size: var(--bk-font-sm);
      }
    `;
