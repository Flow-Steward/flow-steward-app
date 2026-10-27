import { PlatformElement } from '../platform-base.js';

class PlatformJsonEditor extends PlatformElement {

  static get styles() {
    return `
      :host { display: block; }

      .header {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: var(--bk-gap-sm);
      }
      .title {
        font-size: var(--bk-font-base); font-weight: 600; color: var(--text-1); margin: 0;
      }

      .bk-input {
        width: 100%; padding: 10px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md);
        font-family: var(--bk-mono); font-size: var(--bk-font-sm);
        color: var(--text-1); outline: none; resize: vertical;
        min-height: 120px; line-height: 1.5;
        transition: border-color 0.15s, box-shadow 0.15s;
        tab-size: 2;
      }
      .bk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
      .bk-input--error { border-color: var(--red); }
      .bk-input--error:focus { box-shadow: 0 0 0 2px var(--red-soft); }

      .error {
        font-size: var(--bk-font-xs); color: var(--red); margin-top: 4px;
      }

      .toolbar {
        display: flex; gap: var(--bk-gap-sm); margin-top: var(--bk-gap-xs);
      }
      .toolbar-btn {
        background: none; border: none; cursor: pointer; color: var(--text-3);
        font-size: var(--bk-font-xs); padding: 2px 6px; font-family: inherit;
      }
      .toolbar-btn:hover { color: var(--accent); }
    `;
  }

  constructor() {
    super();
    this._text = '';
    this._error = '';
    this._parsed = null;
  }

  set config(val) {
    this._config = val || {};
    const v = this._config.value;
    if (typeof v === 'string') {
      this._text = v;
    } else {
      this._text = JSON.stringify(v ?? null, null, 2);
    }
    this._error = '';
    this._tryParse();
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  _tryParse() {
    try {
      this._parsed = JSON.parse(this._text);
      this._error = '';
      return true;
    } catch (e) {
      this._error = e.message;
      this._parsed = null;
      return false;
    }
  }

  _render() {
    const c = this._config;
    const title = c.title ? `<div class="header"><h3 class="title">${this.esc(c.title)}</h3></div>` : '';
    const ro = c.readonly ? ' readonly' : '';
    const errorCls = this._error ? ' bk-input--error' : '';
    const errorHtml = this._error ? `<div class="error">${this.esc(this._error)}</div>` : '';
    const toolbar = c.readonly ? '' : `
      <div class="toolbar">
        <button type="button" class="toolbar-btn" data-action="format">${this.esc(this.t('component.json.format', 'Format'))}</button>
        <button type="button" class="toolbar-btn" data-action="compact">${this.esc(this.t('component.json.compact', 'Compact'))}</button>
      </div>`;

    this._shadow.innerHTML = `
      ${title}
      <textarea class="bk-input${errorCls}"${ro}>${this.esc(this._text)}</textarea>
      ${errorHtml}
      ${toolbar}
    `;

    this._bindEvents();
  }

  _bindEvents() {
    const root = this._shadow;
    const ta = root.querySelector('textarea');
    if (!ta) {return;}

    ta.addEventListener('input', () => {
      this._text = ta.value;
    });

    ta.addEventListener('blur', () => {
      if (this._tryParse()) {
        ta.classList.remove('bk-input--error');
        this.emit('platform-change', {
          component_id: this._config?.component_id,
          value: this._parsed,
        });
      } else {
        ta.classList.add('bk-input--error');
      }
      // Update error display
      let errEl = root.querySelector('.error');
      if (this._error) {
        if (!errEl) { errEl = document.createElement('div'); errEl.className = 'error'; ta.after(errEl); }
        errEl.textContent = this._error;
      } else if (errEl) {
        errEl.remove();
      }
    });

    // Tab key inserts spaces
    ta.addEventListener('keydown', e => {
      if (e.key === 'Tab') {
        e.preventDefault();
        const start = ta.selectionStart;
        const end = ta.selectionEnd;
        ta.value = ta.value.substring(0, start) + '  ' + ta.value.substring(end);
        ta.selectionStart = ta.selectionEnd = start + 2;
        this._text = ta.value;
      }
    });

    // Toolbar buttons
    root.querySelectorAll('.toolbar-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.action;
        if (this._tryParse()) {
          if (action === 'format') {
            this._text = JSON.stringify(this._parsed, null, 2);
          } else if (action === 'compact') {
            this._text = JSON.stringify(this._parsed);
          }
          ta.value = this._text;
          ta.classList.remove('bk-input--error');
          const errEl = root.querySelector('.error');
          if (errEl) {errEl.remove();}
        }
      });
    });
  }
}

customElements.define('platform-json-editor', PlatformJsonEditor);
export { PlatformJsonEditor };
