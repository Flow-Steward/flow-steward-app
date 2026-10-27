import { PlatformElement } from '../platform-base.js';

class PlatformCostBreakdown extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .shell {
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        background: var(--surface);
        padding: 20px;
        display: grid;
        gap: 16px;
      }
      .header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .title {
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
      }
      .summary ::slotted(*) {
        margin: 0;
      }
      .body {
        display: grid;
        gap: 12px;
      }
    `;
  }

  _render() {
    const title = this.getAttribute('title') || '';
    this._shadow.innerHTML = `
      <section class="shell">
        <div class="header">
          ${title ? `<h3 class="title">${this.esc(title)}</h3>` : '<slot name="header"></slot>'}
          <div class="summary"><slot name="summary"></slot></div>
        </div>
        <div class="body">
          <slot></slot>
        </div>
      </section>
    `;
  }
}

customElements.define('platform-cost-breakdown', PlatformCostBreakdown);
export { PlatformCostBreakdown };
