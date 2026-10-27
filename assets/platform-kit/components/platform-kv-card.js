import { PlatformElement } from '../platform-base.js';

class PlatformKvCard extends PlatformElement {
  static get observedAttributes() {
    return ['items'];
  }

  static get styles() {
    return `
      :host {
        display: block;
      }
      .card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        overflow: hidden;
      }
      .row {
        display: flex;
        align-items: baseline;
        gap: 12px;
        padding: 10px 16px;
        border-bottom: 1px solid var(--border);
      }
      .row:last-child {
        border-bottom: none;
      }
      .key {
        min-width: 140px;
        font-size: var(--bk-font-sm);
        color: var(--text-3);
        flex-shrink: 0;
      }
      .value {
        font-size: var(--bk-font-sm);
        color: var(--text-1);
        word-break: break-word;
      }
      .empty {
        color: var(--text-3);
        font-size: var(--bk-font-sm);
        padding: 14px 16px;
        text-align: center;
      }
    `;
  }

  attributeChangedCallback() {
    if (this.isConnected) {this._render();}
  }

  _render() {
    const c = this._config || {};
    let items = c.items || [];
    if (!items.length) {
      const rawItems = this.getAttribute('items') || '';
      if (rawItems) {
        try {
          const parsed = JSON.parse(rawItems);
          if (Array.isArray(parsed)) {items = parsed;}
        } catch {
          items = [];
        }
      }
    }

    if (items.length === 0) {
      this._shadow.innerHTML = '<div class="card"><div class="empty">No data</div></div>';
      return;
    }

    const rows = items.map(item =>
      `<div class="row">
        <span class="key">${this.esc(item.key)}</span>
        <span class="value">${this.esc(item.value)}</span>
      </div>`
    ).join('');

    this._shadow.innerHTML = `<div class="card">${rows}</div>`;
  }
}

customElements.define('platform-kv-card', PlatformKvCard);
export { PlatformKvCard };
