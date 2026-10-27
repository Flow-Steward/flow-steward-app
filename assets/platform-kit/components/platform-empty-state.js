import { PlatformElement } from '../platform-base.js';

class PlatformEmptyState extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .empty {
        text-align: center;
        padding: 40px 24px;
      }
      .icon {
        width: 48px;
        height: 48px;
        color: var(--text-3);
        margin: 0 auto 16px;
        opacity: 0.5;
      }
      .title {
        font-size: var(--bk-font-base);
        font-weight: 600;
        color: var(--text-1);
        margin: 0 0 6px;
      }
      .desc {
        font-size: var(--bk-font-sm);
        color: var(--text-3);
        margin: 0;
        max-width: 360px;
        margin-left: auto;
        margin-right: auto;
      }
    `;
  }

  _render() {
    const c = this._config;
    const title = c.title || '';
    const desc = c.description || '';
    const icon = c.icon || 'inbox';

    const iconSvg = this._getIcon(icon);

    this._shadow.innerHTML = `
      <div class="empty">
        ${iconSvg}
        ${title ? `<h3 class="title">${this.esc(title)}</h3>` : ''}
        ${desc ? `<p class="desc">${this.esc(desc)}</p>` : ''}
      </div>
    `;
  }

  _getIcon(name) {
    const icons = {
      inbox: '<path d="M22 12H16L14 15H10L8 12H2"/><path d="M5.45 5.11 2 12V18A2 2 0 0 0 4 20H20A2 2 0 0 0 22 18V12L18.55 5.11A2 2 0 0 0 16.76 4H7.24A2 2 0 0 0 5.45 5.11Z"/>',
      search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
      file: '<path d="M14 2H6A2 2 0 0 0 4 4V20A2 2 0 0 0 6 22H18A2 2 0 0 0 20 20V8Z"/><polyline points="14 2 14 8 20 8"/>',
      alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    };
    const paths = icons[name] || icons.inbox;
    return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
  }
}

customElements.define('platform-empty-state', PlatformEmptyState);
export { PlatformEmptyState };
