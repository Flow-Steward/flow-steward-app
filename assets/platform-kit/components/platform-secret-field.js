import { PlatformElement } from '../platform-base.js';

class PlatformSecretField extends PlatformElement {

  static get styles() {
    return `
      :host { display: block; }

      .bk-field { display: flex; flex-direction: column; gap: 4px; }
      .bk-field-label {
        font-size: var(--bk-font-sm); font-weight: 500; color: var(--text-2);
      }

      .bk-secret-wrap { position: relative; }

      .bk-input {
        width: 100%; padding: 7px 10px; padding-right: 50px;
        background: var(--bg); border: 1px solid var(--border);
        border-radius: var(--r-md); font-size: var(--bk-font-md);
        color: var(--text-1); font-family: inherit; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
      }
      .bk-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
      .bk-input::placeholder { color: var(--text-3); }

      .bk-secret-toggle {
        position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
        background: none; border: none; cursor: pointer; color: var(--text-3);
        font-size: var(--bk-font-xs); padding: 2px 6px;
      }
      .bk-secret-toggle:hover { color: var(--text-1); }
    `;
  }

  constructor() {
    super();
    this._visible = false;
    this._value = '';
  }

  set config(val) {
    this._config = val || {};
    this._value = this._config.value || '';
    this._visible = false;
    if (this.isConnected) {this._render();}
  }
  get config() { return this._config; }

  _render() {
    const c = this._config;
    const label = c.label ? `<label class="bk-field-label">${this.esc(c.label)}</label>` : '';
    const ph = c.placeholder
      ? ` placeholder="${this.esc(c.placeholder)}"`
      : ` placeholder="${this.esc(this.t('component.secret.placeholder', 'Enter secret...'))}"`;
    const type = this._visible ? 'text' : 'password';
    const btnLabel = this._visible
      ? this.t('component.secret.hide', 'Hide')
      : this.t('component.secret.show', 'Show');

    this._shadow.innerHTML = `
      <div class="bk-field">
        ${label}
        <div class="bk-secret-wrap">
          <input type="${type}" class="bk-input" value="${this.esc(this._value)}"${ph}>
          <button type="button" class="bk-secret-toggle">${btnLabel}</button>
        </div>
      </div>
    `;

    this._bindEvents();
  }

  _bindEvents() {
    const root = this._shadow;

    const input = root.querySelector('input');
    if (input) {
      input.addEventListener('input', () => {
        this._value = input.value;
        this.emit('platform-change', {
          component_id: this._config?.component_id,
          value: this._value,
        });
      });
    }

    const toggle = root.querySelector('.bk-secret-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        this._visible = !this._visible;
        this._render();
      });
    }
  }
}

customElements.define('platform-secret-field', PlatformSecretField);
export { PlatformSecretField };
