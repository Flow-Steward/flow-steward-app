import { PlatformElement } from '../platform-base.js';
import { PLATFORM_GRID_STYLES } from './platform-grid-styles.js';

/* ── Safe URL scheme allowlist for link cells (mirrors platform-markdown) ──
   Extension-controlled grid values / link_href templates must never be able to
   produce javascript:, data:, vbscript: or other active-content hrefs. We allow
   only an explicit scheme allowlist; relative URLs (path/hash/query) are OK. */
const SAFE_LINK_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);

function isUnsafeLinkHref(href) {
  const normalized = String(href || '').trim();
  if (!normalized) {
    return true;
  }
  // Protocol-relative URLs (//evil.example) inherit the page origin scheme.
  if (normalized.startsWith('//')) {
    return true;
  }
  // Relative URLs (path, fragment, query) carry no scheme and are safe.
  if (normalized.startsWith('/') || normalized.startsWith('#') || normalized.startsWith('?')) {
    return false;
  }
  const schemeSeparator = normalized.indexOf(':');
  if (schemeSeparator <= 0) {
    // No scheme delimiter before any path/query/fragment => relative.
    return false;
  }
  // A ':' only counts as a scheme separator when it precedes any '/', '?' or '#'.
  const slash = normalized.indexOf('/');
  const query = normalized.indexOf('?');
  const hash = normalized.indexOf('#');
  for (const idx of [slash, query, hash]) {
    if (idx !== -1 && idx < schemeSeparator) {
      return false;
    }
  }
  const scheme = normalized.slice(0, schemeSeparator).trim().toLowerCase();
  return !SAFE_LINK_SCHEMES.has(scheme);
}

/** Return href if it uses a safe scheme, otherwise '#' (neutralized). */
function safeLinkHref(href) {
  return isUnsafeLinkHref(href) ? '#' : String(href || '');
}

class PlatformGrid extends PlatformElement {
  static get styles() {
    return PLATFORM_GRID_STYLES;
  }

  constructor() {
    super();
    this._page = 1;
    this._pageSize = 25;
    this._sort = [];
    this._filters = {};
    this._search = '';
    this._selected = new Set();
    this._data = { items: [], total: 0 };
    this._loading = false;
    this._error = '';
    this._debounceTimer = null;
  }

  set config(val) {
    this._config = val || {};
    const c = this._config;
    if (c.pagination?.page_size_default) {
      this._pageSize = c.pagination.page_size_default;
    }
    if (c.sorting?.default) {
      this._sort = [...c.sorting.default];
    }
    // Load fixture data if present
    if (c.fixture_data) {
      this._data = {
        items: c.fixture_data,
        total: c.fixture_data.length,
      };
    }
    if (this.isConnected) {this._render();}
  }

  get config() { return this._config; }

  /* ── Public API ── */

  /** Set data from external query response */
  setData(payload) {
    // Accept a plain rows array from simple callers.
    if (Array.isArray(payload)) {
      this._data = { items: payload, total: payload.length };
    } else {
      const items = Array.isArray(payload?.items) ? payload.items : [];
      const totalRaw = payload?.total;
      const total = Number.isFinite(totalRaw) ? totalRaw : items.length;
      this._data = { items, total };
    }
    this._pruneSelection();
    this._loading = false;
    this._error = '';
    this._render();
  }

  /** Display an error message */
  setError(message) {
    this._error = message || 'An error occurred';
    this._loading = false;
    this._render();
  }

  /** Inject HTML into the detail drawer (forwarded from parent) */
  setDetailContent(html) {
    // This is consumed by the parent orchestrator to fill a platform-drawer
    // We emit an event so the parent can handle it
    this.emit('platform-detail-content', { html });
  }

  /** Clear all selected rows and re-render. */
  clearSelection() {
    if (this._selected.size === 0) {return;}
    this._selected.clear();
    this._render();
  }

  /* ── Internal helpers ── */

  _normalizeItemId(id) {
    if (id === null || id === undefined) {return '';}
    return String(id);
  }

  _normalizeFieldToken(value, fallback = 'field') {
    const raw = String(value === null || value === undefined ? '' : value).trim();
    let token = raw
      .replaceAll(/[^a-zA-Z0-9_-]+/g, '-')
      .toLowerCase();
    while (token.startsWith('-')) {token = token.slice(1);}
    while (token.endsWith('-')) {token = token.slice(0, -1);}
    return token || fallback;
  }

  _pruneSelection(items = null) {
    if (this._selected.size === 0) {return false;}
    let sourceItems = [];
    if (Array.isArray(items)) {
      sourceItems = items;
    } else if (Array.isArray(this._data.items)) {
      sourceItems = this._data.items;
    }
    const validIds = new Set(sourceItems.map(item => this._getItemId(item)).filter(Boolean));
    let changed = false;
    [...this._selected].forEach(id => {
      const normalizedId = this._normalizeItemId(id);
      if (!validIds.has(normalizedId)) {
        this._selected.delete(id);
        changed = true;
      }
    });
    return changed;
  }

  get _columns() {
    const cols = (this._config.columns || []);
    return cols.filter(col => !col.hidden);
  }

  get _totalPages() {
    return Math.max(1, Math.ceil(this._getRenderState().total / this._pageSize));
  }

  get _hasSelection() {
    return (this._config.bulk_actions || []).length > 0;
  }

  _usesRemoteQuery() {
    const c = this._config || {};
    return !!(c.query_id || c.data?.load_query);
  }

  _normalizeSortValue(value) {
    if (value === null || value === undefined) {
      return { type: 'text', value: '' };
    }
    if (typeof value === 'number') {
      return { type: 'number', value };
    }
    if (typeof value === 'boolean') {
      return { type: 'number', value: value ? 1 : 0 };
    }
    if (value instanceof Date) {
      return { type: 'number', value: value.getTime() };
    }
    const text = String(value);
    const numeric = Number(text);
    if (Number.isFinite(numeric) && text.trim() !== '') {
      return { type: 'number', value: numeric };
    }
    const timestamp = Date.parse(text);
    if (!Number.isNaN(timestamp) && /[-:T/]/.test(text)) {
      return { type: 'number', value: timestamp };
    }
    return { type: 'text', value: text.toLowerCase() };
  }

  _compareSortValues(left, right) {
    if (left.type === right.type) {
      if (left.value < right.value) {return -1;}
      if (left.value > right.value) {return 1;}
      return 0;
    }
    if (left.type === 'number') {return -1;}
    if (right.type === 'number') {return 1;}
    return 0;
  }

  _applyLocalSearch(items, columns, searchCfg) {
    if (!searchCfg.enabled || !this._search) {return items;}
    const query = String(this._search || '').trim().toLowerCase();
    if (!query) {return items;}
    const searchKeys = columns.map(col => col.search_key || col.key).filter(Boolean);
    return items.filter(item => {
      return searchKeys.some(key => {
        const raw = PlatformElement.getPath(item, key);
        return String(raw === null || raw === undefined ? '' : raw).toLowerCase().includes(query);
      });
    });
  }

  _normalizeFilterValue(value) {
    return String(value === null || value === undefined ? '' : value)
      .trim()
      .toLowerCase();
  }

  _applyLocalFilters(items, filterFields) {
    if (!filterFields.length) {return items;}
    return items.filter(item => {
      return filterFields.every(field => {
        const active = this._filters[field.filter_id];
        if (active === null || active === undefined || active === '') {return true;}
        const raw = PlatformElement.getPath(item, field.path || field.filter_id);
        const operator = String(field.operator || 'eq').toLowerCase();
        const activeNorm = this._normalizeFilterValue(active);
        if (operator === 'contains') {
          if (Array.isArray(raw)) {
            return raw.some(entry => this._normalizeFilterValue(entry) === activeNorm);
          }
          return this._normalizeFilterValue(raw).includes(activeNorm);
        }
        if (Array.isArray(raw)) {
          return raw.some(entry => this._normalizeFilterValue(entry) === activeNorm);
        }
        return this._normalizeFilterValue(raw) === activeNorm;
      });
    });
  }

  _applyLocalSort(items) {
    if (!Array.isArray(this._sort) || this._sort.length === 0) {return items;}
    const next = [...items];
    next.sort((left, right) => {
      for (const entry of this._sort) {
        const field = entry?.field;
        if (!field) {continue;}
        const leftVal = this._normalizeSortValue(PlatformElement.getPath(left, field));
        const rightVal = this._normalizeSortValue(PlatformElement.getPath(right, field));
        const comparison = this._compareSortValues(leftVal, rightVal);
        if (comparison < 0) {return entry.direction === 'desc' ? 1 : -1;}
        if (comparison > 0) {return entry.direction === 'desc' ? -1 : 1;}
      }
      return 0;
    });
    return next;
  }

  _getRenderState() {
    const c = this._config || {};
    const columns = this._columns;
    const searchCfg = c.search || {};
    const filterSchema = c.filters_schema || {};
    const filterFields = filterSchema.fields || [];
    const sourceItems = Array.isArray(this._data.items) ? this._data.items : [];
    if (this._usesRemoteQuery()) {
      const totalRaw = Number(this._data.total);
      const total = Number.isFinite(totalRaw) ? totalRaw : sourceItems.length;
      return { items: sourceItems, total };
    }

    let items = [...sourceItems];
    items = this._applyLocalFilters(items, filterFields);
    items = this._applyLocalSearch(items, columns, searchCfg);
    items = this._applyLocalSort(items);
    const total = items.length;
    const totalPages = Math.max(1, Math.ceil(total / this._pageSize));
    if (this._page > totalPages) {this._page = totalPages;}
    const startIdx = Math.max(0, (this._page - 1) * this._pageSize);
    const paged = items.slice(startIdx, startIdx + this._pageSize);
    return { items: paged, total };
  }

  _emitQuery() {
    this._loading = true;
    this._render();
    this.emit('platform-query', {
      query_id: this._config.query_id || this._config.component_id,
      component_id: this._config.component_id,
      context: this._config.data?.context || '',
      params: {
        page: this._page,
        page_size: this._pageSize,
        search: this._search,
        sort: this._sort,
        filters: this._filters,
      },
    });
  }

  _onSearchInput(value) {
    clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => {
      this._search = value;
      this._page = 1;
      this._selected.clear();
      if (this._usesRemoteQuery()) {this._emitQuery();}
      else {this._render();}
    }, 300);
  }

  _onSort(field) {
    const existing = this._sort.find(s => s.field === field);
    if (existing) {
      existing.direction = existing.direction === 'asc' ? 'desc' : 'asc';
    } else {
      this._sort = [{ field, direction: 'asc' }];
    }
    this._page = 1;
    if (this._usesRemoteQuery()) {this._emitQuery();}
    else {this._render();}
  }

  _onPageChange(newPage) {
    if (newPage < 1 || newPage > this._totalPages) {return;}
    this._page = newPage;
    this._selected.clear();
    if (this._usesRemoteQuery()) {this._emitQuery();}
    else {this._render();}
  }

  _onPageSizeChange(size) {
    this._pageSize = size;
    this._page = 1;
    this._selected.clear();
    if (this._usesRemoteQuery()) {this._emitQuery();}
    else {this._render();}
  }

  _onFilterChange(filterId, value) {
    if (value === '' || value === null || value === undefined) {
      delete this._filters[filterId];
    } else {
      this._filters[filterId] = value;
    }
    this._page = 1;
    this._selected.clear();
    if (this._usesRemoteQuery()) {this._emitQuery();}
    else {this._render();}
  }

  _toggleRow(id) {
    const normalizedId = this._normalizeItemId(id);
    if (!normalizedId) {return;}
    if (this._selected.has(normalizedId)) {
      this._selected.delete(normalizedId);
    } else {
      this._selected.add(normalizedId);
    }
    this._render();
  }

  _toggleAll() {
    const visibleItems = this._getRenderState().items;
    const visibleIds = visibleItems.map(item => this._getItemId(item)).filter(Boolean);
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => this._selected.has(id));
    if (allVisibleSelected) {
      visibleIds.forEach(id => this._selected.delete(id));
    } else {
      visibleIds.forEach(id => this._selected.add(id));
    }
    this._render();
  }

  _getItemId(item) {
    // Use configured id_field, then fall back to common id fields
    const idField = this._config?.id_field;
    if (idField && item[idField] !== null && item[idField] !== undefined) {
      return this._normalizeItemId(item[idField]);
    }
    return this._normalizeItemId(item.id ?? item._id ?? item.key ?? JSON.stringify(item));
  }

  _onRowAction(actionId, item, triggerElement = null) {
    this.emit('platform-action', {
      action_id: actionId,
      component_id: this._config.component_id,
      context: this._config.data?.context || '',
      scope: 'row',
      item_ids: [this._getItemId(item)],
      target: item,
      trigger_element: triggerElement,
    });
  }

  _onBulkAction(actionId) {
    this.emit('platform-action', {
      action_id: actionId,
      component_id: this._config.component_id,
      context: this._config.data?.context || '',
      scope: 'bulk',
      item_ids: [...this._selected],
      target: null,
    });
  }

  _onToolbarAction(actionId) {
    this.emit('platform-action', {
      action_id: actionId,
      component_id: this._config.component_id,
      context: this._config.data?.context || '',
      scope: 'toolbar',
      item_ids: [],
      target: null,
    });
  }

  _onRowClick(item) {
    const detail = this._config.detail;
    if (!detail) {return;}
    this.emit('platform-detail', {
      component_id: this._config.component_id,
      row_id: this._getItemId(item),
      detail_config: detail,
    });
  }

  /* ── Cell rendering ── */

  _renderCell(item, col) {
    const raw = PlatformElement.getPath(item, col.key);
    const val = raw ?? '';
    const classes = col.monospace ? ['cell-mono'] : [];
    const wrapTruncated = this._buildCellTruncator(item, col, val);
    const renderers = {
      datetime: () => this._renderDatetimeCell(val, classes, wrapTruncated),
      date: () => this._renderDateCell(val, classes, wrapTruncated),
      number: () => this._renderNumberCell(val, col, classes, wrapTruncated),
      badge: () => this._renderBadgeCell(val, col, classes),
      boolean: () => this._renderBooleanCell(val, classes),
      json_summary: () => this._renderJsonSummaryCell(val, classes),
      link: () => this._renderLinkCell(item, val, col, classes, wrapTruncated),
      id: () => this._renderIdCell(val, classes, wrapTruncated),
    };
    const render = renderers[col.type] || (() => this._renderTextCell(val, col, classes, wrapTruncated));
    return render();
  }

  _buildCellTruncator(item, col, defaultValue) {
    const shouldTruncate = col.truncate === true ||
      (col.truncate !== false && (col.type === 'text' || col.type === 'link'));
    return (content, titleValue = defaultValue) => {
      if (!shouldTruncate) {return content;}
      const tooltipRaw = col.tooltip_key ? PlatformElement.getPath(item, col.tooltip_key) : titleValue;
      const tooltipValue = tooltipRaw === null || tooltipRaw === undefined ? '' : String(tooltipRaw);
      const tooltip = col.truncate_title === false ? '' : ` title="${this.esc(tooltipValue)}"`;
      const width = col.truncate_width ? ` style="--bk-cell-truncate-width:${this.esc(String(col.truncate_width))}"` : '';
      return `<div class="cell-truncate"${width}${tooltip}>${content}</div>`;
    };
  }

  _renderEmptyCell(classes) {
    return `<td class="${classes.join(' ')}"><span style="color:var(--text-3)">-</span></td>`;
  }

  _renderDatetimeCell(val, classes, wrapTruncated) {
    if (!val) {return this._renderEmptyCell(classes);}
    const dateValue = new Date(val);
    const formatted = Number.isNaN(dateValue.getTime())
      ? this.esc(val)
      : dateValue.toLocaleString(undefined, {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    return `<td class="${classes.join(' ')}">${wrapTruncated(formatted)}</td>`;
  }

  _renderDateCell(val, classes, wrapTruncated) {
    if (!val) {return this._renderEmptyCell(classes);}
    const dateValue = new Date(val);
    const formatted = Number.isNaN(dateValue.getTime())
      ? this.esc(val)
      : dateValue.toLocaleDateString(undefined, {
          month: 'short', day: 'numeric', year: 'numeric',
        });
    return `<td class="${classes.join(' ')}">${wrapTruncated(formatted)}</td>`;
  }

  _renderNumberCell(val, col, classes, wrapTruncated) {
    classes.push('cell-number');
    const num = typeof val === 'number' ? val : Number.parseFloat(val);
    let formatted = this.esc(val);
    if (!Number.isNaN(num)) {
      if (col.format === 'currency') {
        formatted = num.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
      } else if (col.format === 'percent') {
        formatted = `${(num * 100).toFixed(1)}%`;
      } else {
        formatted = num.toLocaleString();
      }
    }
    return `<td class="${classes.join(' ')}">${wrapTruncated(formatted)}</td>`;
  }

  _renderBadgeCell(val, col, classes) {
    const map = col.badge_map || {};
    const strVal = String(val);
    const color = map[strVal] || map[strVal.toLowerCase()] || 'neutral';
    return `<td class="${classes.join(' ')}"><span class="badge badge-${this.esc(color)}">${this.esc(strVal)}</span></td>`;
  }

  _renderBooleanCell(val, classes) {
    const boolVal = val === true || val === 'true' || val === 1;
    const color = boolVal ? 'green' : 'neutral';
    const label = boolVal ? 'Yes' : 'No';
    return `<td class="${classes.join(' ')}"><span class="badge badge-${color}">${label}</span></td>`;
  }

  _renderJsonSummaryCell(val, classes) {
    const jsonText = JSON.stringify(val);
    const summary = typeof val === 'object'
      ? jsonText.slice(0, 80)
      : this.esc(String(val).slice(0, 80));
    classes.push('cell-mono');
    const suffix = jsonText.length > 80 ? '...' : '';
    return `<td class="${classes.join(' ')}" title="${this.esc(jsonText)}">${summary}${suffix}</td>`;
  }

  _renderLinkCell(item, val, col, classes, wrapTruncated) {
    let href = String(val || '');
    if (typeof col.link_href === 'string') {
      href = this._resolveLinkHrefTemplate(col.link_href, item);
    }
    const safeHref = safeLinkHref(href);
    const label = col.format ? this._applyFormat(val, col.format) : this.esc(val);
    const target = col.new_tab ? ' target="_blank" rel="noopener"' : '';
    const linkHtml = `<a class="cell-link" href="${this.esc(safeHref)}"${target}>${label}</a>`;
    return `<td class="${classes.join(' ')}">${wrapTruncated(linkHtml)}</td>`;
  }

  _resolveLinkHrefTemplate(template, item) {
    const rawTemplate = String(template || '');
    if (!rawTemplate.includes('{')) {
      return rawTemplate;
    }

    let resolved = '';
    let cursor = 0;
    while (cursor < rawTemplate.length) {
      const open = rawTemplate.indexOf('{', cursor);
      if (open === -1) {
        resolved += rawTemplate.slice(cursor);
        break;
      }

      const close = rawTemplate.indexOf('}', open + 1);
      if (close === -1) {
        resolved += rawTemplate.slice(cursor);
        break;
      }

      resolved += rawTemplate.slice(cursor, open);
      const path = rawTemplate.slice(open + 1, close).trim();
      if (path) {
        const rawPathVal = PlatformElement.getPath(item, path);
        if (rawPathVal !== null && rawPathVal !== undefined) {
          resolved += String(rawPathVal);
        }
      }
      cursor = close + 1;
    }
    return resolved;
  }

  _renderIdCell(val, classes, wrapTruncated) {
    classes.push('cell-id');
    return `<td class="${classes.join(' ')}">${wrapTruncated(this.esc(val))}</td>`;
  }

  _renderTextCell(val, col, classes, wrapTruncated) {
    const display = col.format ? this._applyFormat(val, col.format) : this.esc(val);
    return `<td class="${classes.join(' ')}">${wrapTruncated(display)}</td>`;
  }

  _applyFormat(val, format) {
    if (typeof format === 'function') {return this.esc(format(val));}
    return this.esc(val);
  }

  _getSortIndicator(field) {
    const s = this._sort.find(s => s.field === field);
    if (!s) {return '<span class="sort-indicator">\u2195</span>';}
    return s.direction === 'asc'
      ? '<span class="sort-indicator active">\u2191</span>'
      : '<span class="sort-indicator active">\u2193</span>';
  }

  /* ── Main render ── */

  _render() {
    const c = this._config;
    if (!c.component_id && !c.columns) {
      this._shadow.innerHTML = '';
      return;
    }

    const renderState = this._getRenderState();
    const columns = this._columns;
    const items = renderState.items;
    const total = renderState.total;
    const hasSelection = this._hasSelection;
    const rowActions = c.row_actions || [];
    const bulkActions = c.bulk_actions || [];
    const toolbarActions = c.toolbar_actions || [];
    const searchCfg = c.search || {};
    const filterFields = c.filters_schema?.fields || [];
    const pageSizeOptions = c.pagination?.page_size_options || [10, 25, 50, 100];
    const toolbarHtml = this._renderToolbar(searchCfg, toolbarActions);
    const filterHtml = this._renderFilterBar(filterFields);
    const bulkHtml = this._renderBulkBar(hasSelection, bulkActions);
    const errorHtml = this._renderErrorBar();
    const bodyHtml = this._renderBody(c, items, columns, hasSelection, rowActions);
    const footerHtml = this._renderFooter(items, total, pageSizeOptions);

    this._shadow.innerHTML = `
      <div class="grid">
        ${toolbarHtml}
        ${filterHtml}
        ${bulkHtml}
        ${errorHtml}
        ${bodyHtml}
        ${footerHtml}
      </div>
    `;
    this._bindEvents();
  }

  _renderToolbar(searchCfg, toolbarActions) {
    if (!searchCfg.enabled && toolbarActions.length === 0) {return '';}
    const searchHtml = searchCfg.enabled ? `
      <div class="search">
        <svg class="search-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="7" cy="7" r="4.5"/>
          <line x1="10.5" y1="10.5" x2="14" y2="14"/>
        </svg>
        <input type="text" id="grid-search" name="grid_search" placeholder="${this.esc(searchCfg.placeholder || 'Search...')}"
               value="${this.esc(this._search)}" data-role="search"/>
      </div>` : '';
    const toolButtons = toolbarActions.map(action => this._renderToolbarButton(action)).join('');
    return `<div class="toolbar">${searchHtml}<div class="toolbar-actions">${toolButtons}</div></div>`;
  }

  _renderToolbarButton(action) {
    const cls = ['bk-btn', action.variant || '', action.size || ''].filter(Boolean).join(' ');
    const icon = action.icon
      ? `<svg class="btn-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#icon-${this.esc(action.icon)}"/></svg>`
      : '';
    return `<button class="${cls}" data-toolbar-action="${this.esc(action.action_id)}">${icon}${this.esc(action.label)}</button>`;
  }

  _renderFilterBar(filterFields) {
    if (!filterFields.length) {return '';}
    const fields = filterFields.map(field => this._renderFilterField(field)).join('');
    return `<div class="filter-bar">${fields}</div>`;
  }

  _renderFilterField(field) {
    const currentVal = this._filters[field.filter_id] || '';
    const filterToken = this._normalizeFieldToken(field.filter_id, 'filter');
    const fieldMeta = {
      id: `grid-filter-${filterToken}`,
      name: `grid_filter_${filterToken}`,
    };
    const widget = field.widget === 'select'
      ? this._renderFilterSelect(field, currentVal, fieldMeta)
      : this._renderFilterInput(field, currentVal, fieldMeta);
    return `<div class="filter-field"><span class="filter-label">${this.esc(field.label)}</span>${widget}</div>`;
  }

  _renderFilterSelect(field, currentVal, fieldMeta) {
    const options = (field.props?.options || []).map(option => {
      const optVal = typeof option === 'string' ? option : option.value;
      const optLabel = typeof option === 'string' ? option : option.label;
      const selected = currentVal === optVal ? ' selected' : '';
      return `<option value="${this.esc(optVal)}"${selected}>${this.esc(optLabel)}</option>`;
    }).join('');
    return `<select id="${this.esc(fieldMeta.id)}" name="${this.esc(fieldMeta.name)}" data-filter="${this.esc(field.filter_id)}"><option value="">All</option>${options}</select>`;
  }

  _renderFilterInput(field, currentVal, fieldMeta) {
    return `<input type="text" id="${this.esc(fieldMeta.id)}" name="${this.esc(fieldMeta.name)}" data-filter="${this.esc(field.filter_id)}" value="${this.esc(currentVal)}" placeholder="${this.esc(field.label)}"/>`;
  }

  _renderBulkBar(hasSelection, bulkActions) {
    if (!hasSelection || this._selected.size === 0) {return '';}
    const bulkButtons = bulkActions.map(action => {
      const cls = ['bk-btn', 'sm', action.variant || ''].filter(Boolean).join(' ');
      return `<button class="${cls}" data-bulk-action="${this.esc(action.action_id)}">${this.esc(action.label)}</button>`;
    }).join('');
    return `<div class="bulk-bar"><span class="bulk-count">${this._selected.size} selected</span><div class="bulk-actions">${bulkButtons}</div></div>`;
  }

  _renderErrorBar() {
    if (!this._error) {return '';}
    return `<div class="error-bar">${this.esc(this._error)}</div>`;
  }

  _renderBody(config, items, columns, hasSelection, rowActions) {
    if (this._loading) {
      return '<div class="loading-overlay">Loading...</div>';
    }
    if (items.length === 0) {
      return this._renderEmptyBody(config.empty_state || {});
    }
    return this._renderTableBody(config, items, columns, hasSelection, rowActions);
  }

  _renderEmptyBody(empty) {
    const descHtml = empty.description ? `<p class="empty-desc">${this.esc(empty.description)}</p>` : '';
    return `<div class="empty-state">
      <p class="empty-title">${this.esc(empty.title || 'No data')}</p>
      ${descHtml}
    </div>`;
  }

  _renderTableBody(config, items, columns, hasSelection, rowActions) {
    const header = this._renderTableHeader(config, items, columns, hasSelection, rowActions);
    const rows = items.map(item => this._renderTableRow(item, config, columns, hasSelection, rowActions)).join('');
    return `<div class="table-wrap"><table>
      <thead><tr>${header}</tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  }

  _renderTableHeader(config, items, columns, hasSelection, rowActions) {
    const visibleIds = items.map(item => this._getItemId(item));
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => this._selected.has(id));
    let selectAllCell = '';
    if (hasSelection) {
      const checkedAttr = allVisibleSelected ? 'checked' : '';
      selectAllCell = `<th style="width:36px"><input type="checkbox" id="grid-select-all" name="grid_select_all" aria-label="Select all rows" class="row-checkbox" data-role="select-all" ${checkedAttr}/></th>`;
    }
    const headerCells = columns.map(col => {
      const style = col.width ? `style="width:${this.esc(col.width)}"` : '';
      if (col.sortable) {
        return `<th class="sortable" data-sort="${this.esc(col.key)}" ${style}>${this.esc(col.label)}${this._getSortIndicator(col.key)}</th>`;
      }
      return `<th ${style}>${this.esc(col.label)}</th>`;
    }).join('');
    const stickyClass = config.sticky_row_actions ? ' class="row-actions-sticky"' : '';
    const actionsCell = rowActions.length > 0 ? `<th${stickyClass} style="width:1%"></th>` : '';
    return `${selectAllCell}${headerCells}${actionsCell}`;
  }

  _renderTableRow(item, config, columns, hasSelection, rowActions) {
    const id = this._getItemId(item);
    const checked = this._selected.has(id) ? 'checked' : '';
    const rowToken = this._normalizeFieldToken(id, 'row');
    const selectCell = hasSelection
      ? `<td><input type="checkbox" id="grid-row-${this.esc(rowToken)}" name="grid_row_select[]" aria-label="Select row ${this.esc(id)}" value="${this.esc(id)}" class="row-checkbox" data-row-id="${this.esc(id)}" ${checked}/></td>`
      : '';
    const cells = columns.map(col => this._renderCell(item, col)).join('');
    const visibleActions = rowActions.filter(action => this._isRowActionVisible(action, item));
    const actionsCell = rowActions.length > 0
      ? this._renderRowActionsCell(id, visibleActions, Boolean(config.sticky_row_actions))
      : '';
    const clickable = config.detail ? ' style="cursor:pointer"' : '';
    return `<tr data-item-id="${this.esc(id)}"${clickable}>${selectCell}${cells}${actionsCell}</tr>`;
  }

  _isRowActionVisible(action, item) {
    if (!action.visible_if) {return true;}
    const condition = action.visible_if;
    const val = item[condition.field];
    if (condition.in) {return Array.isArray(condition.in) && condition.in.includes(val);}
    if (condition.not_in) {return !Array.isArray(condition.not_in) || !condition.not_in.includes(val);}
    if ('eq' in condition) {return val === condition.eq;}
    if ('neq' in condition) {return val !== condition.neq;}
    return true;
  }

  _renderRowActionsCell(id, visibleActions, sticky = false) {
    const buttons = visibleActions.map(action => {
      const cls = ['bk-btn', 'sm', action.variant || 'ghost'].filter(Boolean).join(' ');
      return `<button class="${cls}" data-row-action="${this.esc(action.action_id)}" data-row-id="${this.esc(id)}">${this.esc(action.label)}</button>`;
    }).join('');
    const className = sticky ? ' class="row-actions-sticky"' : '';
    return `<td${className}><div class="row-actions">${buttons}</div></td>`;
  }

  _renderFooter(items, total, pageSizeOptions) {
    const start = items.length > 0 ? ((this._page - 1) * this._pageSize + 1) : 0;
    const end = Math.min(this._page * this._pageSize, total);
    const optionsHtml = pageSizeOptions.map(n =>
      `<option value="${n}" ${n === this._pageSize ? 'selected' : ''}>${n}</option>`
    ).join('');
    return `
      <div class="footer">
        <div class="footer-left">
          <span>${start}-${end} of ${total}</span>
          <select id="grid-page-size" name="grid_page_size" aria-label="Rows per page" class="page-size-select" data-role="page-size">${optionsHtml}</select>
          <span>per page</span>
        </div>
        <div class="footer-right">
          <button class="page-btn" aria-label="First page" data-page="first" ${this._page <= 1 ? 'disabled' : ''}>\u00AB</button>
          <button class="page-btn" aria-label="Previous page" data-page="prev" ${this._page <= 1 ? 'disabled' : ''}>\u2039</button>
          <span class="page-info">Page ${this._page} of ${this._totalPages}</span>
          <button class="page-btn" aria-label="Next page" data-page="next" ${this._page >= this._totalPages ? 'disabled' : ''}>\u203A</button>
          <button class="page-btn" aria-label="Last page" data-page="last" ${this._page >= this._totalPages ? 'disabled' : ''}>\u00BB</button>
        </div>
      </div>
    `;
  }

  _bindEvents() {
    const shadow = this._shadow;
    const sourceItems = Array.isArray(this._data.items) ? this._data.items : [];
    this._bindSearchEvents(shadow);
    this._bindSortEvents(shadow);
    this._bindSelectionEvents(shadow);
    this._bindDetailRowEvents(shadow, sourceItems);
    this._bindRowActionEvents(shadow, sourceItems);
    this._bindBulkActionEvents(shadow);
    this._bindToolbarActionEvents(shadow);
    this._bindPaginationEvents(shadow);
    this._bindPageSizeEvents(shadow);
    this._bindFilterEvents(shadow);
  }

  _bindSearchEvents(shadow) {
    const searchInput = shadow.querySelector('[data-role="search"]');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => this._onSearchInput(e.target.value));
    }
  }

  _bindSortEvents(shadow) {
    shadow.querySelectorAll('th.sortable').forEach(th => {
      th.addEventListener('click', () => this._onSort(th.dataset.sort));
    });
  }

  _bindSelectionEvents(shadow) {
    const selectAll = shadow.querySelector('[data-role="select-all"]');
    if (selectAll) {
      selectAll.addEventListener('change', () => this._toggleAll());
    }

    // Row checkboxes
    shadow.querySelectorAll('.row-checkbox[data-row-id]').forEach(cb => {
      cb.addEventListener('change', (e) => {
        e.stopPropagation();
        this._toggleRow(cb.dataset.rowId);
      });
    });
  }

  _bindDetailRowEvents(shadow, sourceItems) {
    if (this._config.detail) {
      shadow.querySelectorAll('tbody tr[data-item-id]').forEach(tr => {
        tr.addEventListener('click', (e) => {
          // Don't trigger on checkbox or action button clicks
          if (e.target.closest('.row-checkbox') || e.target.closest('.bk-btn')) {return;}
          const id = tr.dataset.itemId;
          const item = sourceItems.find(i => String(this._getItemId(i)) === id);
          if (item) {this._onRowClick(item);}
        });
      });
    }
  }

  _bindRowActionEvents(shadow, sourceItems) {
    shadow.querySelectorAll('[data-row-action]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = btn.dataset.rowId;
        const item = sourceItems.find(i => String(this._getItemId(i)) === id);
        if (item) {this._onRowAction(btn.dataset.rowAction, item, btn);}
      });
    });
  }

  _bindBulkActionEvents(shadow) {
    shadow.querySelectorAll('[data-bulk-action]').forEach(btn => {
      btn.addEventListener('click', () => this._onBulkAction(btn.dataset.bulkAction));
    });
  }

  _bindToolbarActionEvents(shadow) {
    shadow.querySelectorAll('[data-toolbar-action]').forEach(btn => {
      btn.addEventListener('click', () => this._onToolbarAction(btn.dataset.toolbarAction));
    });
  }

  _resolvePageTarget(pageKey) {
    const pageTargets = {
      first: 1,
      prev: this._page - 1,
      next: this._page + 1,
      last: this._totalPages,
    };
    return pageTargets[pageKey] ?? null;
  }

  _bindPaginationEvents(shadow) {
    shadow.querySelectorAll('.page-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const targetPage = this._resolvePageTarget(btn.dataset.page);
        if (targetPage !== null) {
          this._onPageChange(targetPage);
        }
      });
    });
  }

  _bindPageSizeEvents(shadow) {
    const pageSizeSelect = shadow.querySelector('[data-role="page-size"]');
    if (pageSizeSelect) {
      pageSizeSelect.addEventListener('change', (e) => {
        this._onPageSizeChange(Number.parseInt(e.target.value, 10));
      });
    }
  }

  _bindFilterEvents(shadow) {
    shadow.querySelectorAll('[data-filter]').forEach(el => {
      const event = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(event, (e) => {
        this._onFilterChange(el.dataset.filter, e.target.value);
      });
    });
  }
}

customElements.define('platform-grid', PlatformGrid);
export { PlatformGrid, SAFE_LINK_SCHEMES, isUnsafeLinkHref, safeLinkHref };
