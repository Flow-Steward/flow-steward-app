import { PlatformElement } from '../platform-base.js';

class PlatformHealthPanel extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .panel {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        overflow: hidden;
      }
      .panel-header {
        padding: 12px 16px;
        font-size: var(--bk-font-sm);
        font-weight: 600;
        color: var(--text-1);
        border-bottom: 1px solid var(--border);
      }
      .check {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 16px;
        border-bottom: 1px solid var(--border);
      }
      .check:last-child {
        border-bottom: none;
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
      }
      .dot.healthy   { background: var(--green); }
      .dot.warning   { background: var(--amber); }
      .dot.error     { background: var(--red); }
      .dot.unhealthy { background: var(--red); }
      .dot.unknown   { background: var(--neutral); }
      .check-name {
        font-size: var(--bk-font-sm);
        font-weight: 500;
        color: var(--text-1);
        flex-shrink: 0;
      }
      .check-detail {
        font-size: var(--bk-font-xs, 12px);
        color: var(--text-3);
        margin-left: auto;
        text-align: right;
      }
      .loading {
        color: var(--text-3);
        font-size: var(--bk-font-sm);
        padding: 14px 16px;
      }
    `;
  }

  _render() {
    const c = this._config;
    const checks = c.checks;
    const queryId = c.query_id || c.data?.load_query;

    // Query-driven: emit and show loading
    if (!checks && queryId) {
      this._shadow.innerHTML = '<div class="loading">Loading...</div>';
      this.emit('platform-query', {
        query_id: queryId,
        component_id: c.component_id,
      });
      return;
    }

    const title = c.title
      ? `<div class="panel-header">${this.esc(c.title)}</div>`
      : '';

    if (!checks || checks.length === 0) {
      this._shadow.innerHTML = `<div class="panel">${title}<div class="loading">No checks available</div></div>`;
      return;
    }

    const rows = checks.map(ch => {
      const status = ch.status || 'unknown';
      const detail = ch.detail
        ? `<span class="check-detail">${this.esc(ch.detail)}</span>`
        : '';
      return `
        <div class="check">
          <span class="dot ${this.esc(status)}"></span>
          <span class="check-name">${this.esc(ch.name)}</span>
          ${detail}
        </div>
      `;
    }).join('');

    this._shadow.innerHTML = `<div class="panel">${title}${rows}</div>`;
  }
}

customElements.define('platform-health-panel', PlatformHealthPanel);
export { PlatformHealthPanel };
