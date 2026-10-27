import { PlatformElement } from '../platform-base.js';

class PlatformBadge extends PlatformElement {
  static get observedAttributes() {
    return ['text', 'color', 'title'];
  }

  static get styles() {
    return `
      :host {
        display: inline-flex;
      }
      .badge {
        display: inline-flex;
        align-items: center;
        padding: 2px 10px;
        font-size: var(--bk-font-xs, 12px);
        font-weight: 500;
        line-height: 1.6;
        border-radius: 999px;
        border: 1px solid;
        white-space: nowrap;
      }
      .badge.green {
        background: var(--green-soft);
        color: var(--green);
        border-color: var(--green-border);
      }
      .badge.red {
        background: var(--red-soft);
        color: var(--red);
        border-color: var(--red-border);
      }
      .badge.amber {
        background: var(--amber-soft);
        color: var(--amber);
        border-color: var(--amber-border);
      }
      .badge.gray {
        background: var(--neutral-soft);
        color: var(--neutral);
        border-color: var(--neutral-border);
      }
      .badge.blue {
        background: var(--accent-soft);
        color: var(--accent);
        border-color: var(--accent-border);
      }
    `;
  }

  attributeChangedCallback() {
    if (this.isConnected) {this._render();}
  }

  _render() {
    const c = this._config || {};
    const color = this.getAttribute('color') || c.color || 'gray';
    const text = this.getAttribute('text') || c.text || '';
    const title = this.getAttribute('title') || c.title || '';
    const titleAttr = title ? ` title="${this.esc(title)}"` : '';

    this._shadow.innerHTML = `<span class="badge ${this.esc(color)}"${titleAttr}>${this.esc(text)}</span>`;
  }
}

customElements.define('platform-badge', PlatformBadge);
export { PlatformBadge };
