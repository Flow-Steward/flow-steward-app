import { PlatformElement } from '../platform-base.js';

class PlatformFieldset extends PlatformElement {

  static get styles() {
    return `
      :host { display: block; }

      .bk-section {
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--r-lg); overflow: hidden;
      }
      .bk-section-header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 14px 18px; user-select: none;
      }
      .bk-section-title {
        font-size: var(--bk-font-base); font-weight: 600; color: var(--text-1); margin: 0;
      }
      .bk-section-description {
        font-size: var(--bk-font-sm); color: var(--text-3); margin: 2px 0 0;
      }
      .bk-section-body { padding: 0 18px 18px; }

      .bk-columns { display: grid; gap: var(--bk-gap-lg); }
      .bk-columns--1 { grid-template-columns: 1fr; }
      .bk-columns--2 { grid-template-columns: 1fr 1fr; }
      @media (max-width: 640px) { .bk-columns--2 { grid-template-columns: 1fr; } }

      .bk-field { display: flex; flex-direction: column; gap: 4px; }
      .bk-field-label {
        font-size: var(--bk-font-sm); font-weight: 500; color: var(--text-2);
        display: flex; align-items: center; gap: 4px;
      }
      .bk-field-required { color: var(--red); font-weight: 400; }
      .bk-field-description { font-size: var(--bk-font-xs); color: var(--text-3); }
      .bk-field-help { font-size: var(--bk-font-xs); color: var(--text-3); font-style: italic; }
      .bk-field-error { font-size: var(--bk-font-xs); color: var(--red); }

      .bk-input {
        width: 100%; padding: 7px 10px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md); font-size: var(--bk-font-md);
        color: var(--text-1); font-family: inherit; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
      }
      .bk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
      .bk-input::placeholder { color: var(--text-3); }
      textarea.bk-input { resize: vertical; min-height: 80px; }
      select.bk-input {
        appearance: none;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2366748d' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
        background-repeat: no-repeat; background-position: right 10px center; padding-right: 28px;
      }
      .bk-input--mono { font-family: var(--bk-mono); font-size: var(--bk-font-sm); }

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

      .slot-area { padding: 0 18px 18px; }
    `;
  }

  constructor() {
    super();
    this._values = {};
  }

  set config(val) {
    this._config = val || {};
    if (this._config.values) {
      this._values = PlatformElement.clone(this._config.values);
    }
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  _getValue(path) { return PlatformElement.getPath(this._values, path); }
  _setValue(path, value) { PlatformElement.setPath(this._values, path, value); }

  _normalizeControlToken(value, fallback = 'field') {
    const raw = String(value === null || value === undefined ? '' : value).trim();
    let token = raw
      .replaceAll(/[^a-zA-Z0-9_-]+/g, '-')
      .toLowerCase();
    while (token.startsWith('-')) {token = token.slice(1);}
    while (token.endsWith('-')) {token = token.slice(0, -1);}
    return token || fallback;
  }

  _buildControlMeta(pathValue) {
    const rawPath = String(pathValue === null || pathValue === undefined ? '' : pathValue);
    const token = this._normalizeControlToken(rawPath, 'field');
    return {
      rawPath,
      path: this.esc(rawPath),
      id: this.esc(`bk-field-${token}`),
      name: this.esc(rawPath || token),
    };
  }

  _render() {
    const c = this._config;
    const fields = c.fields || [];
    const layout = c.layout || {};
    const cols = layout.columns || 1;
    const hasComponents = (c.components?.length || 0) > 0;

    const title = c.title ? `<h3 class="bk-section-title">${this.esc(c.title)}</h3>` : '';
    const desc = c.description ? `<p class="bk-section-description">${this.esc(c.description)}</p>` : '';
    const hasHeader = c.title || c.description;

    let fieldsHtml = '';
    if (fields.length) {
      const inner = fields.map(f => this._renderField(f)).join('');
      fieldsHtml = `<div class="bk-columns bk-columns--${cols}">${inner}</div>`;
    }
    const slotHtml = this._renderSlotArea(hasComponents, fields.length > 0);

    this._shadow.innerHTML = `
      <div class="bk-section">
        ${hasHeader ? `<div class="bk-section-header"><div>${title}${desc}</div></div>` : ''}
        ${fieldsHtml ? `<div class="bk-section-body">${fieldsHtml}</div>` : ''}
        ${slotHtml}
      </div>`;

    this._bindEvents();
  }

  _renderField(field) {
    const controlMeta = this._buildControlMeta(field.path);
    const value = this._getValue(controlMeta.rawPath);
    const requiredMark = field.required_display ? '<span class="bk-field-required">*</span>' : '';
    const label = field.label
      ? `<label class="bk-field-label" for="${controlMeta.id}">${this.esc(field.label)}${requiredMark}</label>` : '';
    const desc = field.description ? `<div class="bk-field-description">${this.esc(field.description)}</div>` : '';
    const help = field.help ? `<div class="bk-field-help">${this.esc(field.help)}</div>` : '';
    const ph = field.placeholder ? ` placeholder="${this.esc(field.placeholder)}"` : '';
    const ro = field.readonly ? ' readonly' : '';
    const widget = this._renderFieldWidget(field, value, ph, ro, controlMeta);

    return `<div class="bk-field">${label}${desc}${widget}${help}</div>`;
  }

  _renderSlotArea(hasComponents, hasFields) {
    if (hasComponents) {
      return '<div class="slot-area"><slot></slot></div>';
    }
    if (!hasFields) {
      return '<div class="bk-section-body"><slot></slot></div>';
    }
    return '';
  }

  _renderSelectOptions(field, value) {
    const options = field.props?.options || field.options || [];
    return options.map(option => {
      const optValue = typeof option === 'string' ? option : option.value;
      const optLabel = typeof option === 'string' ? option : (option.label || option.value);
      return `<option value="${this.esc(optValue)}"${optValue === value ? ' selected' : ''}>${this.esc(optLabel)}</option>`;
    }).join('');
  }

  _renderFieldWidget(field, value, placeholderAttr, readonlyAttr, controlMeta) {
    const widget = field.widget || 'text';
    const path = controlMeta.path;
    const escapedValue = this.esc(value ?? '');
    const selectors = {
      text: () => this._renderTextInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, 'text', controlMeta.id, controlMeta.name),
      url: () => this._renderTextInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, 'url', controlMeta.id, controlMeta.name),
      email: () => this._renderTextInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, 'email', controlMeta.id, controlMeta.name),
      textarea: () => `<textarea class="bk-input" id="${controlMeta.id}" name="${controlMeta.name}" data-path="${path}"${placeholderAttr}${readonlyAttr}>${escapedValue}</textarea>`,
      boolean: () => {
        const active = value ? ' active' : '';
        return `<div class="bk-toggle" data-path="${path}" data-widget="boolean">
          <div class="bk-toggle-track${active}"><div class="bk-toggle-thumb"></div></div></div>`;
      },
      select: () => {
        const opts = this._renderSelectOptions(field, value);
        return `<select class="bk-input" id="${controlMeta.id}" name="${controlMeta.name}" data-path="${path}"${field.readonly ? ' disabled' : ''}>
          <option value="">-- Select --</option>${opts}</select>`;
      },
      number: () => this._renderNumericInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, false, controlMeta.id, controlMeta.name),
      integer: () => this._renderNumericInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, true, controlMeta.id, controlMeta.name),
    };
    const render = selectors[widget] || selectors.text;
    return render();
  }

  _renderTextInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, type, controlId, controlName) {
    return `<input type="${type}" class="bk-input"
      id="${controlId}" name="${controlName}" data-path="${path}" value="${escapedValue}"${placeholderAttr}${readonlyAttr}>`;
  }

  _renderNumericInputWidget(path, escapedValue, placeholderAttr, readonlyAttr, integerMode, controlId, controlName) {
    return `<input type="number" class="bk-input" data-path="${path}"
      id="${controlId}" name="${controlName}" value="${escapedValue}"${integerMode ? ' step="1"' : ''}${placeholderAttr}${readonlyAttr}>`;
  }

  _bindEvents() {
    const root = this._shadow;

    root.querySelectorAll('input[data-path], textarea[data-path], select[data-path]').forEach(el => {
      const evt = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(evt, () => {
        this._setValue(el.dataset.path, el.value);
        this.emit('platform-change', {
          component_id: this._config.component_id,
          path: el.dataset.path,
          value: el.value,
          values: PlatformElement.clone(this._values),
        });
      });
    });

    root.querySelectorAll('[data-widget="boolean"]').forEach(el => {
      el.addEventListener('click', () => {
        const path = el.dataset.path;
        const cur = !!this._getValue(path);
        this._setValue(path, !cur);
        el.querySelector('.bk-toggle-track').classList.toggle('active', !cur);
        this.emit('platform-change', {
          component_id: this._config.component_id,
          path,
          value: !cur,
          values: PlatformElement.clone(this._values),
        });
      });
    });
  }
}

customElements.define('platform-fieldset', PlatformFieldset);
export { PlatformFieldset };
