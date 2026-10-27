import { PlatformElement } from '../platform-base.js';

class PlatformLogList extends PlatformElement {
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
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border);
      }
      .panel-title {
        font-size: var(--bk-font-sm);
        font-weight: 600;
        color: var(--text-1);
      }
      .count {
        font-size: var(--bk-font-xs, 12px);
        color: var(--text-3);
        background: var(--hover);
        padding: 2px 8px;
        border-radius: 999px;
      }
      .entries {
        max-height: 400px;
        overflow-y: auto;
      }
      .entry {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        padding: 8px 16px;
        border-bottom: 1px solid var(--border);
        font-size: var(--bk-font-sm);
      }
      .entry:last-child {
        border-bottom: none;
      }
      .sev-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        flex-shrink: 0;
        margin-top: 6px;
      }
      .sev-dot.info    { background: var(--accent); }
      .sev-dot.warning { background: var(--amber); }
      .sev-dot.error   { background: var(--red); }
      .sev-dot.debug   { background: var(--neutral); }
      .timestamp {
        font-size: var(--bk-font-xs, 12px);
        color: var(--text-3);
        font-family: var(--bk-mono, monospace);
        white-space: nowrap;
        flex-shrink: 0;
      }
      .message {
        color: var(--text-2);
        word-break: break-word;
        line-height: 1.4;
      }
      .empty {
        color: var(--text-3);
        font-size: var(--bk-font-sm);
        padding: 14px 16px;
        text-align: center;
      }
    `;
  }

  _render() {
    const c = this._config;
    const title = c.title || 'Logs';
    const maxItems = c.max_items || 100;
    let items = c.items || [];

    if (items.length > maxItems) {
      items = items.slice(-maxItems);
    }

    const header = `
      <div class="panel-header">
        <span class="panel-title">${this.esc(title)}</span>
        <span class="count">${items.length}</span>
      </div>
    `;

    if (items.length === 0) {
      this._shadow.innerHTML = `<div class="panel">${header}<div class="empty">No log entries</div></div>`;
      return;
    }

    const rows = items.map(item => {
      const sev = item.severity || 'info';
      const ts = item.timestamp
        ? `<span class="timestamp">${this.esc(this._formatTime(item.timestamp))}</span>`
        : '';
      return `
        <div class="entry">
          <span class="sev-dot ${this.esc(sev)}"></span>
          ${ts}
          <span class="message">${this.esc(item.message)}</span>
        </div>
      `;
    }).join('');

    this._shadow.innerHTML = `<div class="panel">${header}<div class="entries">${rows}</div></div>`;
  }

  _formatTime(ts) {
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return ts;
    }
  }
}

customElements.define('platform-log-list', PlatformLogList);
export { PlatformLogList };
