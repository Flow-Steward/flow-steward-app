import { PlatformElement } from '../platform-base.js';

class PlatformStatsRow extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .grid {
        display: grid;
        gap: var(--bk-gap-md, 12px);
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      }
      .title {
        font-size: var(--bk-font-sm, 12px);
        font-weight: 600;
        color: var(--text-1);
        margin: 0 0 8px;
      }
      .card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        padding: 14px 16px;
      }
      .stat-label {
        font-size: var(--bk-font-xs, 12px);
        color: var(--text-3);
        margin: 0 0 4px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .stat-value {
        font-size: 1.5em;
        font-weight: 700;
        color: var(--text-1);
        line-height: 1.2;
        margin: 0;
      }
      .stat-trend {
        font-size: var(--bk-font-xs, 12px);
        margin-top: 4px;
      }
      .stat-trend.up {
        color: var(--green);
      }
      .stat-trend.down {
        color: var(--red);
      }
      .stat-trend.flat {
        color: var(--text-3);
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
    const items = c.items;
    const title = c.title ? `<div class="title">${this.esc(c.title)}</div>` : '';
    const queryId = c.query_id || c.data?.load_query;

    // If query-driven and no items yet, emit query and show loading
    if (!items && queryId) {
      this._shadow.innerHTML = `${title}<div class="loading">Loading...</div>`;
      this.emit('platform-query', {
        query_id: queryId,
        component_id: c.component_id,
      });
      return;
    }

    if (!items || items.length === 0) {
      this._shadow.innerHTML = `${title}<div class="loading">No summary available</div>`;
      return;
    }

    const cards = items.map(item => {
      const dir = item.trend_direction || 'flat';
      let arrow = '';
      if (dir === 'up') {
        arrow = '\u2191';
      } else if (dir === 'down') {
        arrow = '\u2193';
      }
      const trend = item.trend
        ? `<div class="stat-trend ${this.esc(dir)}">${arrow} ${this.esc(item.trend)}</div>`
        : '';
      return `
        <div class="card">
          <div class="stat-label">${this.esc(item.label)}</div>
          <div class="stat-value">${this.esc(item.value)}</div>
          ${trend}
        </div>
      `;
    }).join('');

    this._shadow.innerHTML = `${title}<div class="grid">${cards}</div>`;
  }
}

customElements.define('platform-stats-row', PlatformStatsRow);
export { PlatformStatsRow };
