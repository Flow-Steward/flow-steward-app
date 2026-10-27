import { PlatformElement } from '../platform-base.js';

class PlatformKeyValue extends PlatformElement {

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

      .row {
        display: flex; align-items: center; gap: var(--bk-gap-sm);
        margin-bottom: var(--bk-gap-xs);
      }

      .bk-input {
        flex: 1; padding: 7px 10px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md); font-size: var(--bk-font-md);
        color: var(--text-1); font-family: inherit; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
      }
      .bk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
      .bk-input::placeholder { color: var(--text-3); }
      .bk-input--key { flex: 0 0 40%; }

      .bk-btn {
        display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        padding: 7px 14px; border-radius: var(--r-md); font-size: var(--bk-font-sm);
        font-weight: 500; font-family: inherit; cursor: pointer;
        border: 1px solid transparent; transition: all 0.15s; white-space: nowrap;
      }
      .bk-btn--secondary { background: var(--surface); color: var(--text-1); border-color: var(--border); }
      .bk-btn--secondary:hover:not(:disabled) { background: var(--hover); }
      .bk-btn--ghost { background: transparent; color: var(--text-3); border-color: transparent; }
      .bk-btn--ghost:hover:not(:disabled) { background: var(--hover); color: var(--text-1); }
      .bk-btn--sm { padding: 4px 10px; font-size: var(--bk-font-xs); }

      .empty {
        text-align: center; padding: 16px; color: var(--text-3);
        font-size: var(--bk-font-sm);
      }

      .sep { color: var(--text-3); font-size: var(--bk-font-sm); flex-shrink: 0; }
    `;
  }

  constructor() {
    super();
    this._pairs = [];
  }

  set config(val) {
    this._config = val || {};
    this._pairs = this._normalizePairs(this._config.pairs || this._config.values);
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  _normalizePairs(input) {
    if (Array.isArray(input)) {return PlatformElement.clone(input);}
    if (input && typeof input === 'object') {
      return Object.entries(input).map(([key, value]) => ({ key, value: String(value ?? '') }));
    }
    return [];
  }

  _render() {
    const c = this._config;
    const title = c.title ? `<div class="header"><h3 class="title">${this.esc(c.title)}</h3></div>` : '';

    let rows;
    if (this._pairs.length > 0) {
      rows = this._pairs.map((p, i) => `
        <div class="row">
          <input type="text" class="bk-input bk-input--key" value="${this.esc(p.key)}"
            placeholder="Key" data-index="${i}" data-part="key">
          <span class="sep">=</span>
          <input type="text" class="bk-input" value="${this.esc(p.value ?? '')}"
            placeholder="Value" data-index="${i}" data-part="value">
          <button type="button" class="bk-btn bk-btn--ghost bk-btn--sm" data-delete="${i}">&times;</button>
        </div>
      `).join('');
    } else {
      rows = '<div class="empty">No entries. Click "Add" to create one.</div>';
    }

    this._shadow.innerHTML = `
      ${title}
      <div class="rows">${rows}</div>
      <button type="button" class="bk-btn bk-btn--secondary bk-btn--sm" data-add style="margin-top:var(--bk-gap-sm)">+ Add</button>
    `;

    this._bindEvents();
  }

  _emitChange() {
    this.emit('platform-change', {
      component_id: this._config?.component_id,
      pairs: PlatformElement.clone(this._pairs),
    });
  }

  _bindEvents() {
    const root = this._shadow;

    root.querySelectorAll('input[data-index]').forEach(el => {
      el.addEventListener('input', () => {
        const idx = Number.parseInt(el.dataset.index, 10);
        const part = el.dataset.part;
        if (this._pairs[idx]) {
          this._pairs[idx][part] = el.value;
          this._emitChange();
        }
      });
    });

    root.querySelectorAll('[data-delete]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = Number.parseInt(btn.dataset.delete, 10);
        this._pairs.splice(idx, 1);
        this._emitChange();
        this._render();
      });
    });

    const addBtn = root.querySelector('[data-add]');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        this._pairs.push({ key: '', value: '' });
        this._emitChange();
        this._render();
      });
    }
  }
}

customElements.define('platform-key-value', PlatformKeyValue);
export { PlatformKeyValue };
