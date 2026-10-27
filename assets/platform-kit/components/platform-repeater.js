import { PlatformElement } from '../platform-base.js';

class PlatformRepeater extends PlatformElement {

  static get styles() {
    return `
      :host { display: block; }

      .header {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: var(--bk-gap-md);
      }
      .title {
        font-size: var(--bk-font-base); font-weight: 600; color: var(--text-1); margin: 0;
      }
      .description {
        font-size: var(--bk-font-sm); color: var(--text-3); margin: 2px 0 0;
      }

      .item {
        background: var(--content-bg); border: 1px solid var(--border);
        border-radius: var(--r-md); padding: 14px; margin-bottom: var(--bk-gap-sm);
        position: relative;
      }
      .item-fields { display: flex; flex-direction: column; gap: var(--bk-gap-md); }
      .item-actions {
        display: flex; justify-content: flex-end; margin-top: var(--bk-gap-sm);
      }

      .bk-field { display: flex; flex-direction: column; gap: 4px; }
      .bk-field-label {
        font-size: var(--bk-font-sm); font-weight: 500; color: var(--text-2);
        display: flex; align-items: center; gap: 4px;
      }
      .bk-field-description { font-size: var(--bk-font-xs); color: var(--text-3); }

      .bk-input {
        width: 100%; padding: 7px 10px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md); font-size: var(--bk-font-md);
        color: var(--text-1); font-family: inherit; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
      }
      .bk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
      .bk-input::placeholder { color: var(--text-3); }
      textarea.bk-input { resize: vertical; min-height: 60px; }
      select.bk-input {
        appearance: none;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2366748d' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
        background-repeat: no-repeat; background-position: right 10px center; padding-right: 28px;
      }

      .bk-btn {
        display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        padding: 7px 14px; border-radius: var(--r-md); font-size: var(--bk-font-sm);
        font-weight: 500; font-family: inherit; cursor: pointer;
        border: 1px solid transparent; transition: all 0.15s; white-space: nowrap;
      }
      .bk-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      .bk-btn--secondary { background: var(--surface); color: var(--text-1); border-color: var(--border); }
      .bk-btn--secondary:hover:not(:disabled) { background: var(--hover); }
      .bk-btn--danger { background: var(--red-soft); color: var(--red); border-color: var(--red-border); }
      .bk-btn--danger:hover:not(:disabled) { background: var(--red); color: #fff; }
      .bk-btn--sm { padding: 4px 10px; font-size: var(--bk-font-xs); }

      .empty {
        text-align: center; padding: 24px; color: var(--text-3);
        font-size: var(--bk-font-sm);
      }
    `;
  }

  constructor() {
    super();
    this._items = [];
  }

  set config(val) {
    this._config = val || {};
    this._items = PlatformElement.clone(this._config.items || this._config.values || []);
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  _render() {
    const c = this._config;
    const itemUi = c.item_ui || {};
    const fields = itemUi.fields || [];

    const title = c.title ? `<h3 class="title">${this.esc(c.title)}</h3>` : '';
    const desc = c.description ? `<p class="description">${this.esc(c.description)}</p>` : '';
    const hasHeader = c.title || c.description;

    let itemsHtml;
    if (this._items.length > 0) {
      itemsHtml = this._items.map((item, i) => {
        const fieldHtmls = fields.map(f => {
          const val = item[f.path || f.field_id] ?? '';
          return this._renderField(f, val, i);
        }).join('');
        return `<div class="item" data-index="${i}">
          <div class="item-fields">${fieldHtmls}</div>
          <div class="item-actions">
            <button type="button" class="bk-btn bk-btn--danger bk-btn--sm" data-delete="${i}">Remove</button>
          </div>
        </div>`;
      }).join('');
    } else {
      itemsHtml = '<div class="empty">No items. Click "Add" to create one.</div>';
    }

    this._shadow.innerHTML = `
      ${hasHeader ? `<div class="header"><div>${title}${desc}</div></div>` : ''}
      <div class="items">${itemsHtml}</div>
      <button type="button" class="bk-btn bk-btn--secondary" data-add>+ Add Item</button>
    `;

    this._bindEvents();
  }

  _renderField(field, value, itemIndex) {
    const label = field.label
      ? `<label class="bk-field-label">${this.esc(field.label)}</label>` : '';
    const desc = field.description
      ? `<div class="bk-field-description">${this.esc(field.description)}</div>` : '';
    const ph = field.placeholder ? ` placeholder="${this.esc(field.placeholder)}"` : '';
    const fpath = field.path || field.field_id;
    const w = field.widget || 'text';

    let widget;
    switch (w) {
      case 'textarea':
        widget = `<textarea class="bk-input" data-item="${itemIndex}" data-field="${this.esc(fpath)}"${ph}>${this.esc(value)}</textarea>`;
        break;
      case 'number':
      case 'integer':
        widget = `<input type="number" class="bk-input" data-item="${itemIndex}" data-field="${this.esc(fpath)}"
          value="${this.esc(value)}"${w === 'integer' ? ' step="1"' : ''}${ph}>`;
        break;
      case 'select': {
        const opts = (field.props?.options || field.options || []).map(o => {
          const v = typeof o === 'string' ? o : o.value;
          const l = typeof o === 'string' ? o : (o.label || o.value);
          return `<option value="${this.esc(v)}"${v === value ? ' selected' : ''}>${this.esc(l)}</option>`;
        }).join('');
        widget = `<select class="bk-input" data-item="${itemIndex}" data-field="${this.esc(fpath)}">
          <option value="">-- Select --</option>${opts}</select>`;
        break;
      }
      default:
        widget = `<input type="${w === 'url' || w === 'email' ? w : 'text'}" class="bk-input"
          data-item="${itemIndex}" data-field="${this.esc(fpath)}" value="${this.esc(value)}"${ph}>`;
    }

    return `<div class="bk-field">${label}${desc}${widget}</div>`;
  }

  _emitChange() {
    this.emit('platform-change', {
      component_id: this._config?.component_id,
      items: PlatformElement.clone(this._items),
    });
  }

  _bindEvents() {
    const root = this._shadow;

    // Input changes
    root.querySelectorAll('input[data-item], textarea[data-item], select[data-item]').forEach(el => {
      const evt = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(evt, () => {
        const idx = Number.parseInt(el.dataset.item, 10);
        const field = el.dataset.field;
        if (this._items[idx]) {
          this._items[idx][field] = el.value;
          this._emitChange();
        }
      });
    });

    // Delete
    root.querySelectorAll('[data-delete]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = Number.parseInt(btn.dataset.delete, 10);
        this._items.splice(idx, 1);
        this._emitChange();
        this._render();
      });
    });

    // Add
    const addBtn = root.querySelector('[data-add]');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        this._items.push({});
        this._emitChange();
        this._render();
      });
    }
  }
}

customElements.define('platform-repeater', PlatformRepeater);
export { PlatformRepeater };
