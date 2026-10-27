import { PlatformElement } from '../platform-base.js';

class PlatformAlert extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .alert {
        padding: 12px 16px;
        border-radius: var(--r-md);
        font-size: var(--bk-font-sm);
        line-height: 1.5;
        border: 1px solid;
      }
      .title {
        font-weight: 600;
        color: inherit;
        margin: 0 0 6px;
      }
      .message {
        margin: 0;
      }
      .items {
        margin: 8px 0 0;
        padding-left: 18px;
      }
      .items li + li {
        margin-top: 4px;
      }
      .alert.info {
        background: var(--accent-soft);
        color: var(--accent);
        border-color: var(--accent-border);
      }
      .alert.success {
        background: var(--green-soft);
        color: var(--green);
        border-color: var(--green-border);
      }
      .alert.warning {
        background: var(--amber-soft);
        color: var(--amber);
        border-color: var(--amber-border);
      }
      .alert.error {
        background: var(--red-soft);
        color: var(--red);
        border-color: var(--red-border);
      }
    `;
  }

  _render() {
    const c = this._config;
    const level = c.level || 'info';
    const title = c.title || '';
    const message = c.message || '';
    const items = Array.isArray(c.items) ? c.items : [];

    const titleHtml = title ? `<div class="title">${this.esc(title)}</div>` : '';
    const messageHtml = message ? `<div class="message">${this.esc(message)}</div>` : '';
    const itemsHtml = items.length
      ? `<ul class="items">${items.map((item) => {
          if (item && typeof item === 'object') {
            const severity = item.severity ? `[${this.esc(item.severity)}] ` : '';
            const msg = this.esc(item.message || '');
            const hint = item.hint ? ` (${this.esc(item.hint)})` : '';
            return `<li>${severity}${msg}${hint}</li>`;
          }
          return `<li>${this.esc(item)}</li>`;
        }).join('')}</ul>`
      : '';

    this._shadow.innerHTML = `<div class="alert ${this.esc(level)}">${titleHtml}${messageHtml}${itemsHtml}</div>`;
  }
}

customElements.define('platform-alert', PlatformAlert);
export { PlatformAlert };
