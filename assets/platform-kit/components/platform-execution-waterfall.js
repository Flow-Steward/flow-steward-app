import { PlatformElement } from '../platform-base.js';

class PlatformExecutionWaterfall extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .shell {
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        background: var(--surface);
        padding: 18px;
        display: grid;
        gap: 16px;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        flex-wrap: wrap;
      }
      .title {
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
      }
      .body {
        position: relative;
      }
      .body::before {
        content: "";
        position: absolute;
        left: 14px;
        top: 10px;
        bottom: 10px;
        width: 1px;
        background: var(--border);
        pointer-events: none;
      }
      .body ::slotted(*) {
        position: relative;
        z-index: 1;
      }
    `;
  }

  _render() {
    const title = this.getAttribute('title') || '';
    this._shadow.innerHTML = `
      <section class="shell">
        <div class="header">
          ${title ? `<h3 class="title">${this.esc(title)}</h3>` : '<slot name="header"></slot>'}
          <slot name="meta"></slot>
        </div>
        <div class="body">
          <slot></slot>
        </div>
      </section>
    `;
  }
}

customElements.define('platform-execution-waterfall', PlatformExecutionWaterfall);
export { PlatformExecutionWaterfall };
