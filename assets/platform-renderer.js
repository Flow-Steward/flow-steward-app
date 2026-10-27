/**
 * Platform Extension UI Renderer v2
 *
 * Thin manifest adapter that creates platform-* Web Component elements
 * instead of building HTML strings. All rendering logic lives in
 * the component modules (platform-kit/).
 *
 * Usage:
 *   const renderer = new PlatformRenderer(container, compiledManifest);
 *   renderer.render();           // renders first page
 *   renderer.renderPage('config'); // renders specific page
 */

import { MANIFEST_TYPE_MAP } from './platform-kit/index.js';
import {
  FS_COLUMN_TYPES as GLOBAL_FS_COLUMN_TYPES,
  FS_COMPONENT_TYPES as GLOBAL_FS_COMPONENT_TYPES,
  FS_FORM_WIDGETS as GLOBAL_FS_FORM_WIDGETS,
  validateRendererManifest,
} from './platform-renderer-validation.js';

/* ── Condition operators ── */
const CONDITION_OPS = {
  eq:        (v, t) => v === t,
  neq:       (v, t) => v !== t,
  in:        (v, t) => Array.isArray(t) && t.includes(v),
  not_in:    (v, t) => Array.isArray(t) && !t.includes(v),
  exists:    (v)    => v !== undefined && v !== null,
  empty:     (v)    => v === '' || v === null || v === undefined || (Array.isArray(v) && v.length === 0),
  not_empty: (v)    => v !== '' && v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0),
  truthy:    (v)    => !!v,
  falsy:     (v)    => !v,
};

/* ── Container components that hold children ── */
const CONTAINER_TYPES = new Set(['section', 'columns', 'stack', 'fieldset']);

/* ── Executable component types ──
   These component types wire operator-clickable behaviour to onAction /
   action dispatch (see _wireEvents). When a component tree is supplied
   dynamically by an extension query response (e.g. a detail-drawer payload),
   it has NOT passed the compiled manifest / static action-allowlist review,
   so such executable components must be dropped (fail closed) before they are
   instantiated and wired. Display-only components remain allowed. */
const EXECUTABLE_COMPONENT_TYPES = new Set([
  'action_bar', 'filter_bar', 'form', 'confirm_action', 'drawer_detail',
]);

/** True when a component type wires action dispatch and so is unsafe to
 *  instantiate from un-reviewed (extension-query-controlled) payloads. */
function isExecutableComponentType(type) {
  return EXECUTABLE_COMPONENT_TYPES.has(type);
}

/** Recursively drop executable components from an extension-supplied component
 *  tree, returning only display-safe components. Containers are kept but their
 *  children are filtered the same way (fail closed at every level). */
function filterDisplaySafeComponents(components) {
  const safe = [];
  for (const comp of (components || [])) {
    if (!comp || typeof comp !== 'object') {continue;}
    if (isExecutableComponentType(comp.type)) {continue;}
    if (Array.isArray(comp.components) && comp.components.length) {
      safe.push({ ...comp, components: filterDisplaySafeComponents(comp.components) });
    } else {
      safe.push(comp);
    }
  }
  return safe;
}

/* ═══════════════════════════════════════════════════════════════
   PlatformRenderer — manifest walker + event router
   ═══════════════════════════════════════════════════════════════ */
class PlatformRenderer {
  /**
   * @param {HTMLElement} container - DOM element to render into
   * @param {object} manifest - compiled extension manifest
   * @param {object} [opts] - options
   * @param {boolean} [opts.previewMode=false] - show validation errors
   * @param {function} [opts.onAction] - action handler (action_id, payload) => Promise
   * @param {function} [opts.onQuery] - query handler (query_id, params) => Promise
   */
  constructor(container, manifest, opts = {}) {
    this.container = container;
    this.manifest = manifest || {};
    if (!this.manifest.ui_manifest && this.manifest.ui) {
      this.manifest.ui_manifest = this.manifest.ui;
    }
    this.opts = opts;
    this.previewMode = opts.previewMode || false;
    this.onAction = opts.onAction || PlatformRenderer._noopAction;
    this.onQuery = opts.onQuery || PlatformRenderer._noopQuery;

    this.currentPageId = null;
    this._drawerEl = null;
    this._confirmEl = null;
    this._pageEl = null;
    this._localeBundles = this.manifest.locale_bundles || null;
    this._confirmResolve = null;
    this._componentEls = {};  // component_id -> element ref

    // Validate manifest
    this.errors = validateRendererManifest(this.manifest);
  }

  /** Resolve an extension locale string via globalThis.extT or inline fallback. */
  _extT(key, fallback) {
    if (typeof globalThis.extT === 'function') {
      return globalThis.extT(this._localeBundles, key, fallback);
    }
    return fallback || '';
  }

  /** Resolve locale strings on a component config (title, field labels, etc.). */
  _resolveComponentLocale(comp) {
    if (!this._localeBundles || !comp) {
      return comp;
    }
    const cid = comp.component_id || '';
    if (!cid) {
      return comp;
    }
    const resolved = { ...comp };
    if (resolved.title) {
      resolved.title = this._extT('component.' + cid + '.title', resolved.title);
    }
    if (resolved.ui_schema) {
      const schema = { ...resolved.ui_schema };
      if (Array.isArray(schema.sections)) {
        schema.sections = schema.sections.map((s, i) => {
          if (!s.title) {
            return s;
          }
          const sid = s.section_id || String(i);
          return { ...s, title: this._extT('component.' + cid + '.section.' + sid + '.title', s.title) };
        });
      }
      if (Array.isArray(schema.fields)) {
        schema.fields = schema.fields.map(f => {
          if (!f.field_id) {
            return f;
          }
          const rf = { ...f };
          if (rf.label) {
            rf.label = this._extT('component.' + cid + '.field.' + f.field_id + '.label', rf.label);
          }
          if (rf.help) {
            rf.help = this._extT('component.' + cid + '.field.' + f.field_id + '.help', rf.help);
          }
          if (rf.placeholder) {
            rf.placeholder = this._extT(
              'component.' + cid + '.field.' + f.field_id + '.placeholder',
              rf.placeholder,
            );
          }
          return rf;
        });
      }
      if (schema.actions?.save_label) {
        schema.actions = { ...schema.actions, save_label: this._extT('component.' + cid + '.save_label', schema.actions.save_label) };
      }
      resolved.ui_schema = schema;
    }
    if (resolved.data?.empty_state) {
      resolved.data = { ...resolved.data, empty_state: this._extT('component.' + cid + '.empty_state', resolved.data.empty_state) };
    }
    return resolved;
  }

  static _noopAction(actionId, payload) {
    console.warn('[PlatformRenderer] No action handler for:', actionId, payload);
    return Promise.resolve({ ok: true, message: 'No handler (preview mode)' });
  }

  static _noopQuery(queryId, params) {
    console.warn('[PlatformRenderer] No query handler for:', queryId, params);
    return Promise.resolve({ ok: true, items: [], total: 0 });
  }

  /* ── Static helpers (preserved public API) ── */

  static clone(value) {
    const source = value ?? {};
    const cloneFn = globalThis.structuredClone;
    if (typeof cloneFn === 'function') {
      try {
        return cloneFn(source);
      } catch {
        return source;
      }
    }
    return source;
  }

  static esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  static evalCondition(condition, data) {
    if (!condition?.rules) {return true;}
    const match = condition.match || 'all';
    const results = condition.rules.map(rule => {
      const val = PlatformRenderer._getPath(data, rule.path);
      const op = CONDITION_OPS[rule.op];
      return op ? op(val, rule.value) : true;
    });
    return match === 'any' ? results.some(Boolean) : results.every(Boolean);
  }

  static _getPath(obj, path) {
    if (!path) {return obj;}
    return path.split('.').reduce((o, k) => (o !== null && o !== undefined ? o[k] : undefined), obj);
  }

  static _setPath(obj, path, value) {
    const keys = path.split('.');
    let cur = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      if (cur[keys[i]] === null || cur[keys[i]] === undefined) {cur[keys[i]] = {};}
      cur = cur[keys[i]];
    }
    cur[keys[keys.length - 1]] = value;
  }

  static formatDatetime(v) {
    if (!v) {return '\u2014';}
    try {
      const d = new Date(v);
      return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch { return String(v); }
  }

  static formatDate(v) {
    if (!v) {return '\u2014';}
    try {
      return new Date(v).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch { return String(v); }
  }

  static formatNumber(v, format) {
    if (v === null || v === undefined) {return '\u2014';}
    if (format === 'ms') {return `${v}ms`;}
    return String(v);
  }

  /* ── Internal helpers ── */

  _componentData(comp) {
    if (!comp || typeof comp !== 'object') {return {};}
    return (comp.data && typeof comp.data === 'object') ? comp.data : {};
  }

  _componentQueryId(comp) {
    if (!comp || typeof comp !== 'object') {return '';}
    const data = this._componentData(comp);
    return String(comp.query_id || data.load_query || comp.load_query || '').trim();
  }

  _componentQueryContext(comp) {
    const data = this._componentData(comp);
    return (data.context && typeof data.context === 'object') ? data.context : {};
  }

  _componentQueryParams(comp) {
    const data = this._componentData(comp);
    return (data.params && typeof data.params === 'object') ? data.params : {};
  }

  _findComponent(componentId) {
    const ui = this.manifest.ui_manifest;
    if (!ui?.pages) {return null;}
    const walk = (nodes) => {
      for (const comp of (nodes || [])) {
        if (comp.component_id === componentId) {return comp;}
        const found = walk(comp.components || []);
        if (found) {return found;}
      }
      return null;
    };
    for (const page of ui.pages) {
      const found = walk(page.components || []);
      if (found) {return found;}
    }
    return null;
  }

  _extractFormValues(response) {
    if (!response || typeof response !== 'object') {return {};}
    if (response.values && typeof response.values === 'object' && !Array.isArray(response.values)) {
      return response.values;
    }
    if (response.item && typeof response.item === 'object' && !Array.isArray(response.item)) {
      return response.item;
    }
    if (Array.isArray(response.items) && response.items.length > 0 && typeof response.items[0] === 'object') {
      return response.items[0];
    }
    if (response.data && typeof response.data === 'object' && !Array.isArray(response.data)) {
      return response.data;
    }
    return {};
  }

  /* ═══════════════════════════════════════════════════════════════
     RENDER — create DOM elements from manifest
     ═══════════════════════════════════════════════════════════════ */

  render(pageId) {
    const ui = this.manifest.ui_manifest;
    if (!ui?.pages) {
      this.container.innerHTML = '';
      if (this.previewMode && this.errors.length) {
        this.container.appendChild(this._createValidationPanel());
      }
      return;
    }

    // Clear previous state
    this.container.innerHTML = '';
    this._componentEls = {};

    const pages = ui.pages;
    const targetPageId = pageId || pages[0]?.page_id;
    this.currentPageId = targetPageId;

    // Create page element
    const pageEl = document.createElement('platform-page');
    pageEl.config = {
      pages: pages.map(p => ({
        page_id: p.page_id,
        title: this._extT('page.' + p.page_id + '.title', p.title),
        description: this._extT('page.' + p.page_id + '.description', p.description || ''),
      })),
      current_page_id: targetPageId,
      active_page_id: targetPageId,
    };
    this._pageEl = pageEl;

    // Page navigation events
    pageEl.addEventListener('platform-navigate', (e) => {
      this.renderPage(e.detail.page_id);
    });

    // Validation panel (preview mode)
    if (this.previewMode && this.errors.length > 0) {
      pageEl.appendChild(this._createValidationPanel());
    }

    // Page content
    const page = pages.find(p => p.page_id === targetPageId);
    if (page) {
      for (const comp of (page.components || [])) {
        const el = this._renderComponent(comp);
        if (el) {pageEl.appendChild(el);}
      }
    } else {
      const alertEl = document.createElement('platform-alert');
      alertEl.config = { level: 'error', message: `Page '${targetPageId}' not found` };
      pageEl.appendChild(alertEl);
    }

    this.container.appendChild(pageEl);

    // Create shared drawer element
    this._drawerEl = document.createElement('platform-drawer');
    this.container.appendChild(this._drawerEl);

    // Create shared confirm element
    this._confirmEl = document.createElement('platform-confirm');
    this.container.appendChild(this._confirmEl);

    // Expose the active renderer for platform-kit diagnostics and integration tests.
    globalThis._platformRenderer = this;

    // Load data for query-driven components
    this._initQueryComponents();
  }

  renderPage(pageId) {
    this.render(pageId);
  }

  /* ── Validation error panel (preview mode) ── */
  _createValidationPanel() {
    const alertEl = document.createElement('platform-alert');
    const errCount = this.errors.filter(e => e.severity === 'error').length;
    const warnCount = this.errors.filter(e => e.severity === 'warning').length;
    let label = [];
    if (errCount) {label.push(`${errCount} error${errCount > 1 ? 's' : ''}`);}
    if (warnCount) {label.push(`${warnCount} warning${warnCount > 1 ? 's' : ''}`);}

    alertEl.config = {
      level: errCount > 0 ? 'error' : 'warning',
      title: `Compile/Validation: ${label.join(', ')}`,
      items: this.errors.map(e => ({
        severity: e.severity,
        message: e.message,
        file: e.file || '',
        path: e.path || '',
        hint: e.hint || '',
      })),
    };
    return alertEl;
  }

  /* ═══════════════════════════════════════════════════════════════
     COMPONENT FACTORY — manifest type -> platform-* element
     ═══════════════════════════════════════════════════════════════ */

  _renderComponent(comp) {
    if (!comp?.type) {return null;}

    const tag = MANIFEST_TYPE_MAP[comp.type];
    if (!tag) {
      const alertEl = document.createElement('platform-alert');
      alertEl.config = { level: 'error', message: `Unsupported component type: '${comp.type}'` };
      return alertEl;
    }

    const el = document.createElement(tag);
    el.config = this._resolveComponentLocale(comp);

    // Track by component_id for later data loading
    if (comp.component_id) {
      this._componentEls[comp.component_id] = el;
    }

    // Recursively append children for container types
    if (CONTAINER_TYPES.has(comp.type) && Array.isArray(comp.components)) {
      for (const child of comp.components) {
        const childEl = this._renderComponent(child);
        if (childEl) {el.appendChild(childEl);}
      }
    }

    // Wire up event listeners based on component type
    this._wireEvents(el, comp);

    return el;
  }

  /* ═══════════════════════════════════════════════════════════════
     EVENT WIRING — route component events to onAction / onQuery
     ═══════════════════════════════════════════════════════════════ */

  _wireEvents(el, comp) {
    const cid = comp.component_id || '';

    switch (comp.type) {
      case 'form':
        this._wireFormEvents(el, comp, cid);
        break;

      case 'grid':
        this._wireGridEvents(el, comp, cid);
        break;

      case 'stats_row':
      case 'health_panel':
      case 'log_list':
        this._wireQueryEvents(el, comp, cid);
        break;

      case 'action_bar':
      case 'filter_bar':
        el.addEventListener('platform-action', (e) => {
          this._handleActionEvent(e.detail, comp).catch((err) => {
            console.error('[PlatformRenderer] action event handler failed:', err);
          });
        });
        break;

      case 'confirm_action':
        el.addEventListener('platform-action', (e) => {
          const actionId = e.detail.action_id || comp.action_id || '';
          if (actionId) {
            this.onAction(actionId, { component_id: cid });
          }
        });
        break;

      case 'drawer_detail':
        el.addEventListener('platform-detail', () => {
          this._openDrawerDetail(comp);
        });
        break;

    }
  }

  _wireFormEvents(el, comp, cid) {
    const data = this._componentData(comp);

    // Save
    el.addEventListener('platform-save', async (e) => {
      await this._handleFormSave(el, comp, cid, data, e.detail.values || {});
    });

    // Validate
    el.addEventListener('platform-validate', async (e) => {
      await this._handleFormValidate(el, comp, cid, data, e.detail.values || {});
    });

    // Test
    el.addEventListener('platform-test', async (e) => {
      await this._handleFormTest(el, comp, cid, data, e.detail.values || {});
    });

    // Form data loading via query
    el.addEventListener('platform-query', async (e) => {
      const queryId = e.detail.query_id || this._componentQueryId(comp);
      if (!queryId) {return;}
      try {
        const response = await this.onQuery(queryId, {
          page_id: this.currentPageId || '',
          component_id: cid,
          context: this._componentQueryContext(comp),
          params: this._componentQueryParams(comp),
        });
        const values = this._extractFormValues(response);
        if (values && typeof values === 'object' && !Array.isArray(values)) {
          if (typeof el.setValues === 'function') {el.setValues(values);}
        }
      } catch (err) {
        console.warn('[PlatformRenderer] form load query failed:', queryId, err);
      }
    });
  }

  _normalizeActionResponse(result) {
    return (result?.response && typeof result.response === 'object') ? result.response : (result || {});
  }

  _collectFieldErrors(source) {
    const fieldErrors = source?.errors || source?.field_errors || source?.fieldErrors;
    return fieldErrors && typeof fieldErrors === 'object' ? fieldErrors : null;
  }

  _applyFieldErrors(el, fieldErrors) {
    if (typeof el.clearErrors === 'function') {el.clearErrors();}
    if (!fieldErrors || typeof el.setFieldError !== 'function') {return;}
    for (const [fid, msg] of Object.entries(fieldErrors)) {
      el.setFieldError(fid, msg);
    }
  }

  _setElementStatus(el, message, status) {
    if (typeof el.setStatus === 'function') {
      el.setStatus(message, status);
    }
  }

  _buildActionPayload(comp, cid, values, saveMode) {
    const payload = {
      page_id: this.currentPageId || '',
      component_id: cid,
      context: this._componentQueryContext(comp),
      values,
      input: values,
    };
    if (saveMode) {
      payload.save_mode = saveMode;
    }
    return payload;
  }

  async _handleFormSave(el, comp, cid, data, values) {
    const actionId = data.save_action || comp.save_action;
    if (!actionId) {return;}
    try {
      const result = await this.onAction(actionId, this._buildActionPayload(comp, cid, values, data.save_mode || 'patch'));
      this._applyActionResult(comp, result);
      const response = this._normalizeActionResponse(result);
      const fieldErrors = this._collectFieldErrors(response);
      this._applyFieldErrors(el, fieldErrors);
      this._setElementStatus(el, fieldErrors ? 'Validation errors' : 'Saved', fieldErrors ? 'error' : 'saved');
    } catch (err) {
      const directFieldErrors = (err && typeof err === 'object') ? err.fieldErrors : null;
      this._applyFieldErrors(el, directFieldErrors || this._collectFieldErrors(err));
      this._setElementStatus(el, `Save failed: ${err.message}`, 'error');
    }
  }

  async _handleFormValidate(el, comp, cid, data, values) {
    const actionId = data.validate_action || comp.validate_action;
    if (!actionId) {return;}
    try {
      const result = await this.onAction(actionId, this._buildActionPayload(comp, cid, values));
      this._applyActionResult(comp, result);
      const response = this._normalizeActionResponse(result);
      const fieldErrors = this._collectFieldErrors(response);
      const hasErrors = !!(fieldErrors && Object.keys(fieldErrors).length);
      this._applyFieldErrors(el, fieldErrors);
      this._setElementStatus(el, hasErrors ? 'Validation failed' : (response.message || 'Validation passed'), hasErrors ? 'error' : 'saved');
    } catch (err) {
      const directFieldErrors = (err && typeof err === 'object') ? err.fieldErrors : null;
      this._applyFieldErrors(el, directFieldErrors || this._collectFieldErrors(err));
      this._setElementStatus(el, `Validation failed: ${err.message}`, 'error');
    }
  }

  async _handleFormTest(el, comp, cid, data, values) {
    const actionId = data.test_action || comp.test_action;
    if (!actionId) {return;}
    if (typeof el.setStatus === 'function') {
      el.setStatus('Testing...', 'saving');
    } else {
      this._setElementStatus(el, 'Testing...', 'saving');
    }
    try {
      const result = await this.onAction(actionId, this._buildActionPayload(comp, cid, values));
      this._applyActionResult(comp, result);
      const response = this._normalizeActionResponse(result);
      const ok = !!response.ok;
      this._setElementStatus(el, ok ? 'Connection OK' : 'Test failed', ok ? 'saved' : 'error');
    } catch (err) {
      this._setElementStatus(el, `Test failed: ${err.message}`, 'error');
    }
  }

  _wireGridEvents(el, comp, cid) {
    // Query (load data)
    el.addEventListener('platform-query', async (e) => {
      const queryId = e.detail.query_id || this._componentQueryId(comp);
      if (!queryId) {
        // Use fixture data if no query
        if (comp.fixture_data && typeof el.setData === 'function') {
          el.setData(comp.fixture_data);
        }
        return;
      }
      try {
        const queryParams = this._componentQueryParams(comp);
        const detailParams = e.detail.params && typeof e.detail.params === 'object' ? e.detail.params : null;
        const response = await this.onQuery(queryId, {
          page_id: this.currentPageId || '',
          component_id: cid,
          context: this._componentQueryContext(comp),
          params: detailParams ? { ...queryParams, ...detailParams } : queryParams,
        });
        if (typeof el.setData === 'function') {el.setData(response);}
      } catch (err) {
        console.warn('[PlatformRenderer] grid query failed:', queryId, err);
        if (typeof el.setError === 'function') {el.setError(err.message || 'Failed to load');}
      }
    });

    // Row/toolbar/bulk actions
    el.addEventListener('platform-action', (e) => {
      this._handleActionEvent(e.detail, comp).catch((err) => {
        console.error('[PlatformRenderer] action event handler failed:', err);
      });
    });

    // Detail (row click)
    el.addEventListener('platform-detail', async (e) => {
      const detail = comp.detail;
      if (!detail) {return;}
      const rowData = e.detail.row || {};
      const rowId = e.detail.row_id || '';
      const title = detail.title || comp.title || 'Details';

      // Open drawer/modal with loading state
      if (detail.mode === 'modal') {
        this.openModal(title, '<div class="bk-loading"><div class="bk-spinner"></div> Loading detail...</div>');
      } else {
        this.openDrawer(title, '<div class="bk-loading"><div class="bk-spinner"></div> Loading detail...</div>');
      }

      let payload = rowData;
      const queryId = String(detail.query_id || '').trim();
      if (queryId) {
        try {
          const detailParams = (detail.params && typeof detail.params === 'object') ? detail.params : {};
          const response = await this.onQuery(queryId, {
            page_id: this.currentPageId || '',
            component_id: cid,
            context: this._componentQueryContext(comp),
            params: { ...this._componentQueryParams(comp), ...detailParams,
              item_id: rowId,
              row_id: rowId,
              item_ids: [rowId],
            },
          });
          payload = (response && typeof response === 'object') ? response : { value: response };
        } catch (err) {
          payload = { error: err.message || 'Failed to load detail' };
        }
      }

      this._renderDetailIntoDrawer(payload, detail.mode);
    });
  }

  _wireQueryEvents(el, comp, cid) {
    el.addEventListener('platform-query', async (e) => {
      const queryId = e.detail.query_id || this._componentQueryId(comp);
      if (!queryId) {return;}
      try {
        const response = await this.onQuery(queryId, {
          page_id: this.currentPageId || '',
          component_id: cid,
          context: this._componentQueryContext(comp),
          params: this._componentQueryParams(comp),
        });
        if (typeof el.setData === 'function') {
          el.setData(response);
        } else {
          this._applyQueryResult(comp, el, response);
        }
      } catch (err) {
        console.warn('[PlatformRenderer] component query failed:', queryId, err);
      }
    });
  }

  async _handleActionEvent(detail, comp) {
    const actionId = detail.action_id || '';
    if (!actionId) {return;}
    try {
      const result = await this.onAction(actionId, {
        page_id: this.currentPageId || '',
        component_id: comp.component_id || '',
        context: this._componentQueryContext(comp),
        scope: detail.scope || 'page',
        item_ids: detail.item_ids || [],
        target: { scope: detail.scope || 'page', item_ids: detail.item_ids || [] },
        values: detail.values || undefined,
      });
      this._applyActionResult(comp, result);
    } catch (err) {
      console.error('Action failed:', err);
    }
  }

  /* ═══════════════════════════════════════════════════════════════
     DATA LOADING — trigger initial queries on query-driven components
     ═══════════════════════════════════════════════════════════════ */

  _initQueryComponents() {
    const ui = this.manifest.ui_manifest;
    if (!ui?.pages) {return;}
    const page = ui.pages.find(p => p.page_id === this.currentPageId);
    if (!page) {return;}

    this._walkComponents(page.components || [], (comp) => {
      if (!comp?.component_id) {return;}
      const el = this._componentEls[comp.component_id];
      if (!el) {return;}

      const queryId = this._componentQueryId(comp);
      if (!queryId) {return;}

      // Trigger the component's own data load by dispatching its query event
      // Components that support platform-query will fire it on connectedCallback,
      // but we also explicitly trigger for components that need an initial load
      if (typeof el.loadData === 'function') {
        el.loadData();
      } else if (comp.type === 'stats_row' || comp.type === 'health_panel') {
        // These components emit their own platform-query on first render.
        return;
      } else {
        // Fallback: dispatch a synthetic platform-query event
        el.dispatchEvent(new CustomEvent('platform-query', {
          bubbles: true,
          detail: { query_id: queryId },
        }));
      }
    });
  }

  _walkComponents(components, visitor) {
    for (const comp of (components || [])) {
      visitor(comp);
      if (comp && Array.isArray(comp.components) && comp.components.length) {
        this._walkComponents(comp.components, visitor);
      }
    }
  }

  _applyActionResult(sourceComp, result) {
    const payload = (result && typeof result === 'object') ? result : {};
    // Unwrap platform invocation envelope: reload/ok/message live inside .response
    const response = (payload.response && typeof payload.response === 'object') ? payload.response : payload;
    const reload = Array.isArray(response.reload) ? response.reload : [];
    if (!reload.length) {return;}
    for (const componentId of reload) {
      this._reloadComponentById(componentId);
    }
  }

  _applyQueryResult(comp, el, response) {
    const payload = (response && typeof response === 'object') ? response : {};
    if (comp.type === 'stats_row') {
      let items = Array.isArray(payload.items) ? payload.items : [];
      if (!items.length && payload.summary && typeof payload.summary === 'object') {
        items = Object.entries(payload.summary).map(([label, value]) => ({
          label,
          value: value === null || value === undefined ? '' : String(value),
        }));
      }
      el.config = { ...comp, items };
      return;
    }

    if (comp.type === 'health_panel') {
      const checks = this._resolveHealthChecks(payload);
      el.config = { ...comp, checks };
      return;
    }

    if (comp.type === 'log_list') {
      const items = Array.isArray(payload.items) ? payload.items : [];
      el.config = { ...comp, items };
    }
  }

  _resolveHealthChecks(payload) {
    if (Array.isArray(payload.checks)) {return payload.checks;}
    if (Array.isArray(payload.items)) {return payload.items;}
    return [];
  }

  _reloadComponentById(componentId) {
    const el = this._componentEls[componentId];
    const comp = this._findComponent(componentId);
    if (!el || !comp) {return;}

    if (typeof el.loadData === 'function') {
      el.loadData();
    } else {
      const queryId = this._componentQueryId(comp);
      if (queryId) {
        el.dispatchEvent(new CustomEvent('platform-query', {
          bubbles: true,
          detail: { query_id: queryId },
        }));
      }
    }
  }

  /* ═══════════════════════════════════════════════════════════════
     DETAIL RENDERING (drawer/modal payloads)
     ═══════════════════════════════════════════════════════════════ */

  _renderDetailIntoDrawer(payload, mode) {
    const target = this._resolveDetailTarget(mode);
    if (!target) {return;}

    target.innerHTML = '';

    if ((payload === null || payload === undefined) || (Array.isArray(payload) && !payload.length)) {
      const empty = document.createElement('platform-empty-state');
      empty.config = { title: 'No detail' };
      target.appendChild(empty);
      return;
    }

    // If payload has components array, render them. The payload comes from an
    // extension QUERY response and has NOT passed the compiled-manifest static
    // action-allowlist review, so executable (action-wiring) components are
    // dropped here — BEFORE _renderComponent instantiates/wires them — leaving
    // only display-safe components. Fail closed: unknown/executable => dropped.
    const detail = (payload.item && typeof payload.item === 'object') ? payload.item : payload;
    if (detail.components && Array.isArray(detail.components)) {
      const safeComponents = filterDisplaySafeComponents(detail.components);
      for (const comp of safeComponents) {
        const el = this._renderComponent(comp);
        if (el) {target.appendChild(el);}
      }
      return;
    }

    // Fallback: render as key-value card
    const kvEl = document.createElement('platform-kv-card');
    const entries = typeof detail === 'object' && !Array.isArray(detail) ? detail : { value: detail };
    kvEl.config = {
      items: Object.entries(entries).map(([key, value]) => ({
        key,
        value: this._detailValueToString(value),
      })),
    };
    target.appendChild(kvEl);
  }

  _resolveDetailTarget(mode) {
    if (mode === 'modal') {
      return this.container.querySelector('[data-bk="detail-modal-body"]');
    }
    return this._drawerEl?.shadowRoot?.querySelector('[data-bk="drawer-body"]') || null;
  }

  _detailValueToString(value) {
    if (value && typeof value === 'object') {
      return JSON.stringify(value);
    }
    return String(value ?? '');
  }

  _openDrawerDetail(comp) {
    let contentHtml = '';
    if (comp.components) {
      // Build a temp container
      const frag = document.createDocumentFragment();
      for (const child of comp.components) {
        const el = this._renderComponent(child);
        if (el) {frag.appendChild(el);}
      }
      this.openDrawer(comp.title || 'Details', '');
      const body = this._drawerEl?.shadowRoot?.querySelector('[data-bk="drawer-body"]') || null;
      if (body) {
        body.innerHTML = '';
        body.appendChild(frag);
      }
      return;
    }
    if (comp.content) {
      contentHtml = PlatformRenderer.esc(comp.content);
    } else if (comp.data && typeof comp.data === 'object') {
      const kvEl = document.createElement('platform-kv-card');
      kvEl.config = {
        items: Object.entries(comp.data).map(([k, v]) => ({ key: k, value: String(v) })),
      };
      this.openDrawer(comp.title || 'Details', '');
      const body = this._drawerEl?.shadowRoot?.querySelector('[data-bk="drawer-body"]') || null;
      if (body) { body.innerHTML = ''; body.appendChild(kvEl); }
      return;
    }
    this.openDrawer(comp.title || 'Details', contentHtml);
  }

  /* ═══════════════════════════════════════════════════════════════
     DRAWER & CONFIRM UTILITIES
     ═══════════════════════════════════════════════════════════════ */

  openDrawer(title, contentHtml) {
    this.closeModal();
    if (this._drawerEl && typeof this._drawerEl.open === 'function') {
      this._drawerEl.open(title, contentHtml);
    }
  }

  closeDrawer() {
    if (this._drawerEl && typeof this._drawerEl.close === 'function') {
      this._drawerEl.close();
    }
  }

  openModal(title, contentHtml) {
    this.closeDrawer();
    // Use the drawer element in modal mode if supported, otherwise fallback
    if (this._drawerEl && typeof this._drawerEl.openModal === 'function') {
      this._drawerEl.openModal(title, contentHtml);
    }
  }

  closeModal() {
    if (this._drawerEl && typeof this._drawerEl.closeModal === 'function') {
      this._drawerEl.closeModal();
    }
  }

  confirm(title, message, variant = 'danger') {
    if (this._confirmEl && typeof this._confirmEl.show === 'function') {
      return this._confirmEl.show(title, message, variant);
    }
    // Fallback: resolve immediately
    return Promise.resolve(false);
  }

  /* ── Cleanup ── */
  destroy() {
    if (globalThis._platformRenderer === this) {globalThis._platformRenderer = null;}
    this.closeDrawer();
    this.closeModal();
    this.container.innerHTML = '';
    this._componentEls = {};
    this._drawerEl = null;
    this._confirmEl = null;
    this._pageEl = null;
  }
}

/* Export for module use */
export { PlatformRenderer };
export {
  EXECUTABLE_COMPONENT_TYPES,
  isExecutableComponentType,
  filterDisplaySafeComponents,
};
export { FS_COLUMN_TYPES, FS_COMPONENT_TYPES, FS_FORM_WIDGETS } from './platform-renderer-validation.js';

/* Expose globally for platform-kit consumers loaded outside ESM. */
globalThis.PlatformRenderer = PlatformRenderer;
globalThis.FS_COMPONENT_TYPES = GLOBAL_FS_COMPONENT_TYPES;
globalThis.FS_FORM_WIDGETS = GLOBAL_FS_FORM_WIDGETS;
globalThis.FS_COLUMN_TYPES = GLOBAL_FS_COLUMN_TYPES;
