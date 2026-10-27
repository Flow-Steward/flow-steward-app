import { PlatformElement } from '../platform-base.js';

class PlatformStatusSummary extends PlatformElement {
  static get observedAttributes() {
    return ['tone'];
  }

  static get styles() {
    return `
      :host {
        display: block;
      }
      .shell {
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        background: var(--surface);
        padding: 22px;
        display: grid;
        gap: 16px;
      }
      .shell.blue {
        border-color: var(--accent-border);
      }
      .shell.green {
        border-color: var(--green-border);
      }
      .shell.amber {
        border-color: var(--amber-border);
      }
      .shell.red {
        border-color: var(--red-border);
      }
      .topline {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .eyebrow {
        display: grid;
        gap: 6px;
      }
      .eyebrow ::slotted(*) {
        margin: 0;
      }
      .headline {
        display: grid;
        gap: 8px;
      }
      .headline ::slotted([slot="headline"]) {
        font-size: clamp(26px, 4vw, 40px);
        line-height: 1.05;
        color: var(--text-1);
        font-weight: 650;
      }
      .headline ::slotted([slot="summary"]) {
        color: var(--text-2);
        font-size: var(--bk-font-base);
        line-height: 1.6;
      }
      .facts {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px;
      }
      .facts ::slotted(*) {
        display: block;
      }
    `;
  }

  attributeChangedCallback() {
    if (this.isConnected) {this._render();}
  }

  _render() {
    const tone = this.getAttribute('tone') || 'blue';
    this._shadow.innerHTML = `
      <article class="shell ${this.esc(tone)}">
        <div class="topline">
          <div class="eyebrow">
            <slot name="eyebrow"></slot>
          </div>
          <slot name="badge"></slot>
        </div>
        <div class="headline">
          <slot name="headline"></slot>
          <slot name="summary"></slot>
        </div>
        <div class="facts">
          <slot name="facts"></slot>
        </div>
      </article>
    `;
  }
}

customElements.define('platform-status-summary', PlatformStatusSummary);
export { PlatformStatusSummary };
