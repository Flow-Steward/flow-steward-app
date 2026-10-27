import { PlatformElement } from '../platform-base.js';

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

/* ── SVG icons ── */
const CHEVRON_SVG = `<svg class="chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 6 8 10 12 6"/></svg>`;

const SELECT_ARROW = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2366748d' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E";

class PlatformForm extends PlatformElement {

  static get styles() {
    return `
      :host { display: block; }

      /* ── Form ── */
      .bk-form { display: flex; flex-direction: column; gap: var(--bk-gap-xl); }

      /* ── Form Actions ── */
      .bk-form-actions {
        display: flex; align-items: center; gap: var(--bk-gap-sm);
        padding-top: var(--bk-gap-lg); border-top: 1px solid var(--border);
        margin-top: var(--bk-gap-lg);
      }
      .bk-form-status { font-size: var(--bk-font-sm); margin-left: auto; }
      .bk-form-status--saved  { color: var(--green); }
      .bk-form-status--error  { color: var(--red); }
      .bk-form-status--saving { color: var(--text-3); }

      /* ── Section ── */
      .bk-section {
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--r-lg); overflow: hidden;
      }
      .bk-section--plain {
        background: transparent; border: none; border-radius: 0;
      }
      .bk-section--inset {
        background: var(--content-bg); border-color: var(--border);
      }
      .bk-section-header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 14px 18px; cursor: default; user-select: none;
      }
      .bk-section--collapsible .bk-section-header { cursor: pointer; }
      .bk-section--collapsible .bk-section-header:hover { background: var(--hover); }
      .bk-section-title {
        font-size: var(--bk-font-base); font-weight: 600; color: var(--text-1); margin: 0;
      }
      .bk-section-description {
        font-size: var(--bk-font-sm); color: var(--text-3); margin: 2px 0 0;
      }
      .chevron {
        width: 16px; height: 16px; color: var(--text-3);
        transition: transform 0.15s; flex-shrink: 0;
      }
      .bk-section.collapsed .chevron { transform: rotate(-90deg); }
      .bk-section-body { padding: 0 18px 18px; }
      .bk-section.collapsed .bk-section-body { display: none; }

      /* ── Columns ── */
      .bk-columns { display: grid; gap: var(--bk-gap-lg); }
      .bk-columns--1 { grid-template-columns: 1fr; }
      .bk-columns--2 { grid-template-columns: 1fr 1fr; }
      @media (max-width: 640px) { .bk-columns--2 { grid-template-columns: 1fr; } }
      .bk-col-span-2 { grid-column: span 2; }

      /* ── Field ── */
      .bk-field { display: flex; flex-direction: column; gap: 4px; }
      .bk-field-label {
        font-size: var(--bk-font-sm); font-weight: 500; color: var(--text-2);
        display: flex; align-items: center; gap: 4px;
      }
      .bk-field-required { color: var(--red); font-weight: 400; }
      .bk-field-description { font-size: var(--bk-font-xs); color: var(--text-3); }
      .bk-field-help { font-size: var(--bk-font-xs); color: var(--text-3); font-style: italic; }
      .bk-field-error { font-size: var(--bk-font-xs); color: var(--red); }

      /* ── Input ── */
      .bk-input {
        width: 100%; padding: 7px 10px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md); font-size: var(--bk-font-md);
        color: var(--text-1); font-family: inherit; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
      }
      .bk-input:focus {
        border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft);
      }
      .bk-input--error { border-color: var(--red); }
      .bk-input--error:focus { box-shadow: 0 0 0 2px var(--red-soft); }
      .bk-input--mono { font-family: var(--bk-mono); font-size: var(--bk-font-sm); }
      .bk-input::placeholder { color: var(--text-3); }
      textarea.bk-input { resize: vertical; min-height: 80px; }
      select.bk-input {
        appearance: none;
        background-image: url("${SELECT_ARROW}");
        background-repeat: no-repeat;
        background-position: right 10px center;
        padding-right: 28px;
      }

      /* ── Toggle ── */
      .bk-toggle { display: flex; align-items: center; gap: var(--bk-gap-sm); cursor: pointer; }
      .bk-toggle-track {
        position: relative; width: 36px; height: 20px;
        background: var(--neutral-soft); border: 1px solid var(--border);
        border-radius: 10px; transition: background 0.15s, border-color 0.15s; flex-shrink: 0;
      }
      .bk-toggle-track.active { background: var(--accent); border-color: var(--accent); }
      .bk-toggle-thumb {
        position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
        background: #fff; border-radius: 50%; transition: left 0.15s; box-shadow: var(--shadow-sm);
      }
      .bk-toggle-track.active .bk-toggle-thumb { left: 18px; }

      /* ── Secret ── */
      .bk-secret-wrap { position: relative; }
      .bk-secret-toggle {
        position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
        background: none; border: none; cursor: pointer; color: var(--text-3);
        font-size: var(--bk-font-xs); padding: 2px 6px;
      }
      .bk-secret-toggle:hover { color: var(--text-1); }

      /* ── Radio / Checkbox Group ── */
      .bk-choice-group { display: flex; flex-direction: column; gap: var(--bk-gap-sm); }
      .bk-choice-item {
        display: flex; align-items: center; gap: var(--bk-gap-sm);
        font-size: var(--bk-font-md); color: var(--text-1); cursor: pointer;
      }
      .bk-choice-item input { accent-color: var(--accent); cursor: pointer; }

      /* ── Tag Picker ── */
      .bk-tag-picker { display: flex; flex-wrap: wrap; gap: var(--bk-gap-xs); }
      .bk-tag {
        display: inline-flex; align-items: center; gap: 4px;
        padding: 2px 8px; background: var(--accent-soft); border: 1px solid var(--accent-border);
        border-radius: 12px; font-size: var(--bk-font-xs); color: var(--accent);
      }
      .bk-tag-remove {
        cursor: pointer; font-size: 14px; line-height: 1; opacity: 0.6;
      }
      .bk-tag-remove:hover { opacity: 1; }
      .bk-tag-input {
        border: none; background: transparent; outline: none; font-size: var(--bk-font-sm);
        color: var(--text-1); min-width: 80px; padding: 2px 4px; font-family: inherit;
      }
      .bk-tag-input::placeholder { color: var(--text-3); }

      /* ── Buttons ── */
      .bk-btn {
        display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        padding: 7px 14px; border-radius: var(--r-md); font-size: var(--bk-font-sm);
        font-weight: 500; font-family: inherit; cursor: pointer;
        border: 1px solid transparent; transition: all 0.15s; white-space: nowrap;
      }
      .bk-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      .bk-btn--primary { background: var(--accent); color: #fff; border-color: var(--accent); }
      .bk-btn--primary:hover:not(:disabled) { filter: brightness(1.1); }
      .bk-btn--secondary { background: var(--surface); color: var(--text-1); border-color: var(--border); }
      .bk-btn--secondary:hover:not(:disabled) { background: var(--hover); }
      .bk-btn--danger { background: var(--red-soft); color: var(--red); border-color: var(--red-border); }
      .bk-btn--danger:hover:not(:disabled) { background: var(--red); color: #fff; }
      .bk-btn--ghost { background: transparent; color: var(--text-3); border-color: transparent; }
      .bk-btn--ghost:hover:not(:disabled) { background: var(--hover); color: var(--text-1); }
      .bk-btn--sm { padding: 4px 10px; font-size: var(--bk-font-xs); }

      /* ── Loading ── */
      .bk-spinner {
        width: 16px; height: 16px; border: 2px solid var(--border);
        border-top-color: var(--accent); border-radius: 50%;
        animation: bk-spin 0.6s linear infinite; display: inline-block;
      }
      @keyframes bk-spin { to { transform: rotate(360deg); } }
    `;
  }

  constructor() {
    super();
    this._values = {};
    this._dirty = false;
    this._errors = {};
    this._loading = false;
    this._statusText = '';
    this._statusType = '';
    this._collapsedSections = new Set();
    this._secretVisible = new Set();
    this._tagInputs = {};
  }

  set config(val) {
    this._config = val || {};
    // Seed values from config
    if (this._config.values) {
      this._values = PlatformElement.clone(this._config.values);
    }
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  /* ── Public API ── */

  setValues(values) {
    this._values = PlatformElement.clone(values);
    this._dirty = false;
    this._errors = {};
    this._render();
  }

  setStatus(text, type) {
    this._statusText = text || '';
    this._statusType = type || '';
    this._loading = type === 'saving';
    this._updateStatus();
  }

  setFieldError(fieldId, message) {
    if (message) {
      this._errors[fieldId] = message;
    } else {
      delete this._errors[fieldId];
    }
    this._render();
  }

  clearErrors() {
    this._errors = {};
    this._render();
  }

  /* ── Condition evaluation ── */

  _evalCondition(cond) {
    if (!cond?.rules) {return true;}
    const match = cond.match || 'all';
    const results = cond.rules.map(r => {
      const val = PlatformElement.getPath(this._values, r.path);
      const op = CONDITION_OPS[r.op];
      return op ? op(val, r.value) : true;
    });
    return match === 'any' ? results.some(Boolean) : results.every(Boolean);
  }

  /* ── Value helpers ── */

  _getValue(path) {
    return PlatformElement.getPath(this._values, path);
  }

  _setValue(path, value) {
    PlatformElement.setPath(this._values, path, value);
    this._dirty = true;
  }

  /* ── Rendering ── */

  _render() {
    const c = this._config;
    const ui = c.ui_schema || {};
    const sections = ui.sections;
    const fields = ui.fields || [];

    let body = '';
    if (sections?.length) {
      body = sections.map(s => this._renderSection(s)).join('');
    } else if (fields.length) {
      body = this._renderFields(fields, ui.layout);
    }

    const actions = this._renderActions(ui.actions);

    this._shadow.innerHTML = `<div class="bk-form">${body}${actions}</div>`;
    this._bindEvents();
  }

  _renderSection(section) {
    if (!this._evalCondition(section.visible_if)) {return '';}

    const variant = section.variant || 'card';
    const collapsible = !!section.collapsible;
    const collapsed = this._collapsedSections.has(section.section_id);

    const variantCls = variant === 'card' ? '' : ` bk-section--${variant}`;
    const collCls = collapsible ? ' bk-section--collapsible' : '';
    const collapsedCls = collapsed ? ' collapsed' : '';

    const title = section.title
      ? `<h3 class="bk-section-title">${this.esc(section.title)}</h3>` : '';
    const desc = section.description
      ? `<p class="bk-section-description">${this.esc(section.description)}</p>` : '';
    const chevron = collapsible ? CHEVRON_SVG : '';
    const hasHeader = section.title || section.description;

    const header = hasHeader ? `
      <div class="bk-section-header" data-section-id="${this.esc(section.section_id || '')}">
        <div>${title}${desc}</div>
        ${chevron}
      </div>` : '';

    // Resolve field_ids → field objects (v1 manifest uses field_ids references)
    const ui = this._config.ui_schema || {};
    const allFields = ui.fields || [];
    let sectionFields = section.fields;
    if (!sectionFields && section.field_ids) {
      const fieldMap = {};
      for (const f of allFields) {fieldMap[f.field_id] = f;}
      sectionFields = section.field_ids.map(fid => fieldMap[fid]).filter(Boolean);
    }
    const fieldsHtml = this._renderFields(sectionFields || [], section.layout);

    return `
      <div class="bk-section${variantCls}${collCls}${collapsedCls}" data-section="${this.esc(section.section_id || '')}">
        ${header}
        <div class="bk-section-body">${fieldsHtml}</div>
      </div>`;
  }

  _renderFields(fields, layout) {
    const cols = layout?.columns || 1;
    const colCls = `bk-columns bk-columns--${cols}`;
    const inner = fields.map(f => {
      if (!this._evalCondition(f.visible_if)) {return '';}
      const span = (f.column_span && f.column_span > 1) ? ' bk-col-span-2' : '';
      return `<div class="${span}">${this._renderField(f)}</div>`;
    }).join('');
    return `<div class="${colCls}">${inner}</div>`;
  }

  _renderField(field) {
    const fid = field.field_id || field.path;
    const error = this._errors[fid];
    const errorCls = error ? ' bk-input--error' : '';
    const value = this._getValue(field.path);

    let label = '';
    if (field.label) {
      const req = field.required_display
        ? `<span class="bk-field-required">*</span>` : '';
      label = `<label class="bk-field-label">${this.esc(field.label)}${req}</label>`;
    }
    const desc = field.description
      ? `<div class="bk-field-description">${this.esc(field.description)}</div>` : '';
    const help = field.help
      ? `<div class="bk-field-help">${this.esc(field.help)}</div>` : '';
    const errorHtml = error
      ? `<div class="bk-field-error">${this.esc(error)}</div>` : '';

    const widget = this._renderWidget(field, value, errorCls);

    return `<div class="bk-field" data-field-id="${this.esc(fid)}">
      ${label}${desc}${widget}${help}${errorHtml}
    </div>`;
  }

  _renderWidget(field, value, errorCls) {
    const widget = field.widget || 'text';
    const fid = field.field_id || field.path;
    const props = field.props || {};
    const options = props.options || field.options || [];
    const selectorOptions = props.options || [];
    const renderers = {
      text: () => this._renderTextLikeWidget(field, value, errorCls, 'text'),
      url: () => this._renderTextLikeWidget(field, value, errorCls, 'url'),
      email: () => this._renderTextLikeWidget(field, value, errorCls, 'email'),
      number: () => this._renderNumberWidget(field, value, errorCls, 'number', props),
      integer: () => this._renderNumberWidget(field, value, errorCls, 'integer', props),
      textarea: () => this._renderTextareaWidget(field, value, errorCls, props),
      boolean: () => this._renderBooleanWidget(field, value),
      select: () => this._renderSelectWidget(field, value, errorCls, options),
      radio_group: () => this._renderRadioGroupWidget(field, value, options, fid),
      checkbox_group: () => this._renderCheckboxGroupWidget(field, value, options),
      tag_picker: () => this._renderTagPickerWidget(field, value),
      secret: () => this._renderSecretWidget(field, value, errorCls, fid),
      json: () => this._renderJsonWidget(field, value, errorCls, props),
      key_value: () => this._renderKvWidget(field.path, this._toKvArray(value), fid, field.readonly),
      repeater: () => this._renderRepeaterWidget(
        field.path,
        Array.isArray(value) ? value : [],
        props,
        fid,
        field.readonly
      ),
      event_selector: () => this._renderSelectorWidget(field, value, errorCls, selectorOptions, 'event_selector'),
      project_selector: () => this._renderSelectorWidget(field, value, errorCls, selectorOptions, 'project_selector'),
      workflow_selector: () => this._renderSelectorWidget(field, value, errorCls, selectorOptions, 'workflow_selector'),
      provider_selector: () => this._renderSelectorWidget(field, value, errorCls, selectorOptions, 'provider_selector'),
    };
    const render = renderers[widget];
    return render ? render() : this._renderTextLikeWidget(field, value, errorCls, 'text');
  }

  _placeholderAttr(field, fallback = '') {
    if (field.placeholder) {
      return ` placeholder="${this.esc(field.placeholder)}"`;
    }
    return fallback;
  }

  _readonlyAttr(field) {
    return field.readonly ? ' readonly' : '';
  }

  _disabledAttr(field) {
    return field.readonly ? ' disabled' : '';
  }

  _normalizeOption(option) {
    if (typeof option === 'string') {
      return { value: option, label: option };
    }
    return { value: option.value, label: option.label || option.value };
  }

  _renderTextLikeWidget(field, value, errorCls, type) {
    const path = this.esc(field.path);
    const inputType = type === 'text' ? 'text' : type;
    const placeholder = this._placeholderAttr(field);
    const readonly = this._readonlyAttr(field);
    return `<input type="${inputType}" class="bk-input${errorCls}"
      data-path="${path}" value="${this.esc(value ?? '')}"${placeholder}${readonly}>`;
  }

  _renderNumberWidget(field, value, errorCls, widget, props) {
    const path = this.esc(field.path);
    let step = '';
    if (widget === 'integer') {
      step = ' step="1"';
    } else if (props.step) {
      step = ` step="${props.step}"`;
    }
    const min = props.min !== null && props.min !== undefined ? ` min="${props.min}"` : '';
    const max = props.max !== null && props.max !== undefined ? ` max="${props.max}"` : '';
    const placeholder = this._placeholderAttr(field);
    const readonly = this._readonlyAttr(field);
    return `<input type="number" class="bk-input${errorCls}"
      data-path="${path}" value="${this.esc(value ?? '')}"${step}${min}${max}${placeholder}${readonly}>`;
  }

  _renderTextareaWidget(field, value, errorCls, props) {
    const path = this.esc(field.path);
    const rows = props.rows ? ` rows="${props.rows}"` : '';
    const mono = props.mono ? ' bk-input--mono' : '';
    const placeholder = this._placeholderAttr(field);
    const readonly = this._readonlyAttr(field);
    return `<textarea class="bk-input${mono}${errorCls}"
      data-path="${path}"${rows}${placeholder}${readonly}>${this.esc(value ?? '')}</textarea>`;
  }

  _renderBooleanWidget(field, value) {
    const path = this.esc(field.path);
    const active = value ? ' active' : '';
    return `<div class="bk-toggle" data-path="${path}" data-widget="boolean">
      <div class="bk-toggle-track${active}"><div class="bk-toggle-thumb"></div></div>
    </div>`;
  }

  _renderSelectWidget(field, value, errorCls, options) {
    const path = this.esc(field.path);
    const disabled = this._disabledAttr(field);
    const optionsHtml = options.map(option => {
      const normalized = this._normalizeOption(option);
      const selected = normalized.value === value ? ' selected' : '';
      return `<option value="${this.esc(normalized.value)}"${selected}>${this.esc(normalized.label)}</option>`;
    }).join('');
    return `<select class="bk-input${errorCls}" data-path="${path}"${disabled}>
      <option value="">-- Select --</option>${optionsHtml}
    </select>`;
  }

  _renderRadioGroupWidget(field, value, options, fid) {
    const path = this.esc(field.path);
    const disabled = this._disabledAttr(field);
    const optionsHtml = options.map(option => {
      const normalized = this._normalizeOption(option);
      const checked = normalized.value === value ? ' checked' : '';
      return `<label class="bk-choice-item">
        <input type="radio" name="${this.esc(fid)}" value="${this.esc(normalized.value)}"
          data-path="${path}"${checked}${disabled}>
        ${this.esc(normalized.label)}
      </label>`;
    }).join('');
    return `<div class="bk-choice-group">${optionsHtml}</div>`;
  }

  _renderCheckboxGroupWidget(field, value, options) {
    const path = this.esc(field.path);
    const disabled = this._disabledAttr(field);
    const selectedValues = Array.isArray(value) ? value : [];
    const optionsHtml = options.map(option => {
      const normalized = this._normalizeOption(option);
      const checked = selectedValues.includes(normalized.value) ? ' checked' : '';
      return `<label class="bk-choice-item">
        <input type="checkbox" value="${this.esc(normalized.value)}"
          data-path="${path}" data-widget="checkbox_group"${checked}${disabled}>
        ${this.esc(normalized.label)}
      </label>`;
    }).join('');
    return `<div class="bk-choice-group">${optionsHtml}</div>`;
  }

  _renderTagPickerWidget(field, value) {
    const path = this.esc(field.path);
    const readonly = this._readonlyAttr(field);
    const placeholder = this._placeholderAttr(field, ' placeholder="Type and press Enter"');
    const tags = (Array.isArray(value) ? value : []).map((tag, index) => `
      <span class="bk-tag">${this.esc(tag)}<span class="bk-tag-remove" data-path="${path}" data-tag-index="${index}">&times;</span></span>
    `).join('');
    return `<div class="bk-tag-picker" data-path="${path}" data-widget="tag_picker">
      ${tags}
      <input type="text" class="bk-tag-input" data-path="${path}" data-widget="tag_input"${placeholder}${readonly}>
    </div>`;
  }

  _renderSecretWidget(field, value, errorCls, fid) {
    const path = this.esc(field.path);
    const placeholder = this._placeholderAttr(field);
    const readonly = this._readonlyAttr(field);
    const visible = this._secretVisible.has(fid);
    const inputType = visible ? 'text' : 'password';
    const buttonLabel = visible ? 'Hide' : 'Show';
    return `<div class="bk-secret-wrap">
      <input type="${inputType}" class="bk-input${errorCls}"
        data-path="${path}" value="${this.esc(value ?? '')}"${placeholder}${readonly}>
      <button type="button" class="bk-secret-toggle" data-secret-field="${this.esc(fid)}">${buttonLabel}</button>
    </div>`;
  }

  _renderJsonWidget(field, value, errorCls, props) {
    const path = this.esc(field.path);
    const readonly = this._readonlyAttr(field);
    const jsonValue = typeof value === 'string' ? value : JSON.stringify(value ?? null, null, 2);
    const rows = props.rows ? ` rows="${props.rows}"` : ' rows="6"';
    return `<textarea class="bk-input bk-input--mono${errorCls}"
      data-path="${path}" data-widget="json"${rows}${readonly}>${this.esc(jsonValue)}</textarea>`;
  }

  _renderSelectorWidget(field, value, errorCls, options, widget) {
    const path = this.esc(field.path);
    const disabled = this._disabledAttr(field);
    const selectorName = this.esc(widget.replace('_selector', ''));
    const optionsHtml = options.map(option => {
      const normalized = this._normalizeOption(option);
      const selected = normalized.value === value ? ' selected' : '';
      return `<option value="${this.esc(normalized.value)}"${selected}>${this.esc(normalized.label)}</option>`;
    }).join('');
    return `<select class="bk-input${errorCls}" data-path="${path}"${disabled}>
      <option value="">-- Select ${selectorName} --</option>${optionsHtml}
    </select>`;
  }

  /* ── Inline key-value widget ── */

  _toKvArray(value) {
    if (Array.isArray(value)) {return value;}
    if (value && typeof value === 'object') {
      return Object.entries(value).map(([key, val]) => ({ key, value: val }));
    }
    return [];
  }

  _renderKvWidget(path, pairs, fid, readonly) {
    const ro = readonly ? ' readonly' : '';
    const rows = pairs.map((p, i) => `
      <div class="bk-kv-row" style="display:flex;gap:var(--bk-gap-sm);align-items:center;margin-bottom:var(--bk-gap-xs);">
        <input type="text" class="bk-input" style="flex:1" value="${this.esc(p.key)}"
          data-path="${this.esc(path)}" data-kv-index="${i}" data-kv-part="key"${ro}>
        <input type="text" class="bk-input" style="flex:1" value="${this.esc(p.value ?? '')}"
          data-path="${this.esc(path)}" data-kv-index="${i}" data-kv-part="value"${ro}>
        ${readonly ? '' : `<button type="button" class="bk-btn bk-btn--ghost bk-btn--sm" data-kv-delete="${this.esc(path)}" data-kv-index="${i}">&times;</button>`}
      </div>`).join('');
    const addBtn = readonly ? '' : `<button type="button" class="bk-btn bk-btn--ghost bk-btn--sm" data-kv-add="${this.esc(path)}">+ Add</button>`;
    return `<div data-widget="key_value" data-path="${this.esc(path)}">${rows}${addBtn}</div>`;
  }

  /* ── Inline repeater widget ── */

  _renderRepeaterWidget(path, items, props, fid, readonly) {
    const itemFields = props.item_ui?.fields || [];
    const rows = items.map((item, i) => {
      const fieldHtmls = itemFields.map(f => {
        const subPath = `${path}.${i}.${f.path || f.field_id}`;
        const subField = { ...f, path: subPath };
        return this._renderField(subField);
      }).join('');
      const delBtn = readonly ? '' : `<button type="button" class="bk-btn bk-btn--danger bk-btn--sm" data-repeater-delete="${this.esc(path)}" data-repeater-index="${i}">&times; Remove</button>`;
      return `<div class="bk-section bk-section--inset" style="margin-bottom:var(--bk-gap-sm);">
        <div class="bk-section-body" style="padding-top:12px">${fieldHtmls}${delBtn}</div>
      </div>`;
    }).join('');
    const addBtn = readonly ? '' : `<button type="button" class="bk-btn bk-btn--secondary bk-btn--sm" data-repeater-add="${this.esc(path)}">+ Add Item</button>`;
    return `<div data-widget="repeater" data-path="${this.esc(path)}">${rows}${addBtn}</div>`;
  }

  /* ── Actions ── */

  _renderActions(actions) {
    if (!actions) {return '';}
    // Normalize v1 object-shaped actions {save_label, test_label} to array
    if (!Array.isArray(actions)) {
      const arr = [];
      if (actions.save_label) {arr.push({ action: 'save', label: actions.save_label, variant: 'primary' });}
      if (actions.test_label) {arr.push({ action: 'test', label: actions.test_label, variant: 'secondary' });}
      if (actions.validate_label) {arr.push({ action: 'validate', label: actions.validate_label, variant: 'secondary' });}
      actions = arr;
    }
    if (!actions.length) {return '';}
    const btns = actions.map(a => {
      const variant = a.variant || 'primary';
      const dis = this._loading ? ' disabled' : '';
      return `<button type="button" class="bk-btn bk-btn--${variant}" data-action="${this.esc(a.action)}"${dis}>${this.esc(a.label)}</button>`;
    }).join('');
    const spinnerHtml = this._loading ? '<span class="bk-spinner"></span> ' : '';
    const status = this._statusText
      ? `<span class="bk-form-status bk-form-status--${this.esc(this._statusType)}">${spinnerHtml}${this.esc(this._statusText)}</span>`
      : '';
    return `<div class="bk-form-actions">${btns}${status}</div>`;
  }

  _updateStatus() {
    const el = this._shadow.querySelector('.bk-form-status');
    if (!el) {
      // Re-render if actions exist to show status
      const actionsBar = this._shadow.querySelector('.bk-form-actions');
      if (actionsBar) {
        let statusEl = actionsBar.querySelector('.bk-form-status');
        if (!statusEl) {
          statusEl = document.createElement('span');
          actionsBar.appendChild(statusEl);
        }
        statusEl.className = `bk-form-status bk-form-status--${this._statusType}`;
        statusEl.innerHTML = `${this._loading ? '<span class="bk-spinner"></span> ' : ''}${this.esc(this._statusText)}`;
      }
      return;
    }
    el.className = `bk-form-status bk-form-status--${this._statusType}`;
    el.innerHTML = `${this._loading ? '<span class="bk-spinner"></span> ' : ''}${this.esc(this._statusText)}`;

    // Update button disabled state
    this._shadow.querySelectorAll('.bk-form-actions .bk-btn').forEach(btn => {
      btn.disabled = this._loading;
    });
  }

  /* ── Event binding ── */

  _bindEvents() {
    const root = this._shadow;

    // Section collapse toggle
    root.querySelectorAll('.bk-section--collapsible .bk-section-header').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const sec = hdr.closest('.bk-section');
        const sid = sec.dataset.section;
        sec.classList.toggle('collapsed');
        if (sec.classList.contains('collapsed')) {
          this._collapsedSections.add(sid);
        } else {
          this._collapsedSections.delete(sid);
        }
      });
    });

    // Text / number / url / email / select inputs
    root.querySelectorAll('input[data-path]:not([data-kv-part]):not([data-widget]), textarea[data-path]:not([data-widget]), select[data-path]').forEach(el => {
      const evtName = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(evtName, () => {
        this._setValue(el.dataset.path, el.value);
      });
    });

    // JSON textarea
    root.querySelectorAll('textarea[data-widget="json"]').forEach(el => {
      el.addEventListener('input', () => {
        this._dirty = true;
      });
      el.addEventListener('blur', () => {
        try {
          const parsed = JSON.parse(el.value);
          this._setValue(el.dataset.path, parsed);
          delete this._errors[el.dataset.path];
          el.classList.remove('bk-input--error');
        } catch {
          this._errors[el.dataset.path] = 'Invalid JSON';
          el.classList.add('bk-input--error');
        }
        // Update error display
        const fieldEl = el.closest('.bk-field');
        if (fieldEl) {
          let errEl = fieldEl.querySelector('.bk-field-error');
          const errMsg = this._errors[el.dataset.path];
          if (errMsg) {
            if (!errEl) { errEl = document.createElement('div'); errEl.className = 'bk-field-error'; fieldEl.appendChild(errEl); }
            errEl.textContent = errMsg;
          } else if (errEl) {
            errEl.remove();
          }
        }
      });
    });

    // Boolean toggle
    root.querySelectorAll('[data-widget="boolean"]').forEach(el => {
      el.addEventListener('click', () => {
        const path = el.dataset.path;
        const cur = !!this._getValue(path);
        this._setValue(path, !cur);
        const track = el.querySelector('.bk-toggle-track');
        track.classList.toggle('active', !cur);
      });
    });

    // Radio group
    root.querySelectorAll('input[type="radio"][data-path]').forEach(el => {
      el.addEventListener('change', () => {
        if (el.checked) {this._setValue(el.dataset.path, el.value);}
      });
    });

    // Checkbox group
    root.querySelectorAll('input[data-widget="checkbox_group"]').forEach(el => {
      el.addEventListener('change', () => {
        const path = el.dataset.path;
        const cur = this._getValue(path);
        const arr = Array.isArray(cur) ? [...cur] : [];
        if (el.checked) {
          if (!arr.includes(el.value)) {arr.push(el.value);}
        } else {
          const idx = arr.indexOf(el.value);
          if (idx >= 0) {arr.splice(idx, 1);}
        }
        this._setValue(path, arr);
      });
    });

    // Tag picker
    root.querySelectorAll('[data-widget="tag_input"]').forEach(el => {
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const val = el.value.trim();
          if (!val) {return;}
          const path = el.dataset.path;
          const cur = this._getValue(path);
          const arr = Array.isArray(cur) ? [...cur] : [];
          if (!arr.includes(val)) {
            arr.push(val);
            this._setValue(path, arr);
            this._render();
          }
        }
      });
    });

    root.querySelectorAll('.bk-tag-remove').forEach(el => {
      el.addEventListener('click', () => {
        const path = el.dataset.path;
        const idx = Number.parseInt(el.dataset.tagIndex, 10);
        const cur = this._getValue(path);
        const arr = Array.isArray(cur) ? [...cur] : [];
        arr.splice(idx, 1);
        this._setValue(path, arr);
        this._render();
      });
    });

    // Secret toggle
    root.querySelectorAll('.bk-secret-toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        const fid = btn.dataset.secretField;
        if (this._secretVisible.has(fid)) {
          this._secretVisible.delete(fid);
        } else {
          this._secretVisible.add(fid);
        }
        this._render();
      });
    });

    // Key-value inputs
    root.querySelectorAll('input[data-kv-part]').forEach(el => {
      el.addEventListener('input', () => {
        const path = el.dataset.path;
        const idx = Number.parseInt(el.dataset.kvIndex, 10);
        const part = el.dataset.kvPart;
        const cur = this._getValue(path);
        const pairs = this._toKvArray(cur);
        if (pairs[idx]) {
          pairs[idx][part] = el.value;
          this._setValue(path, pairs);
        }
      });
    });
    root.querySelectorAll('[data-kv-delete]').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = btn.dataset.kvDelete;
        const idx = Number.parseInt(btn.dataset.kvIndex, 10);
        const cur = this._getValue(path);
        const pairs = this._toKvArray(cur);
        pairs.splice(idx, 1);
        this._setValue(path, pairs);
        this._render();
      });
    });
    root.querySelectorAll('[data-kv-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = btn.dataset.kvAdd;
        const cur = this._getValue(path);
        const pairs = this._toKvArray(cur);
        pairs.push({ key: '', value: '' });
        this._setValue(path, pairs);
        this._render();
      });
    });

    // Repeater add/delete
    root.querySelectorAll('[data-repeater-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = btn.dataset.repeaterAdd;
        const cur = this._getValue(path);
        const arr = Array.isArray(cur) ? [...cur] : [];
        arr.push({});
        this._setValue(path, arr);
        this._render();
      });
    });
    root.querySelectorAll('[data-repeater-delete]').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = btn.dataset.repeaterDelete;
        const idx = Number.parseInt(btn.dataset.repeaterIndex, 10);
        const cur = this._getValue(path);
        const arr = Array.isArray(cur) ? [...cur] : [];
        arr.splice(idx, 1);
        this._setValue(path, arr);
        this._render();
      });
    });

    // Action buttons
    root.querySelectorAll('.bk-form-actions .bk-btn[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.action;
        const c = this._config;
        const data = c.data || {};
        const payload = {
          component_id: c.component_id,
          values: PlatformElement.clone(this._values),
          context: data.context,
        };
        if (action === 'save') {
          payload.save_mode = data.save_mode;
          this.emit('platform-save', payload);
        } else if (action === 'validate') {
          this.emit('platform-validate', payload);
        } else if (action === 'test') {
          this.emit('platform-test', payload);
        }
      });
    });
  }
}

customElements.define('platform-form', PlatformForm);
export { PlatformForm };
