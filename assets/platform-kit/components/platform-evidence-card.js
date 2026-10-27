import { PlatformElement } from '../platform-base.js';

class PlatformEvidenceCard extends PlatformElement {
  static get observedAttributes() {
    return ['tone'];
  }

  static get styles() {
    return `
      :host {
        display: block;
      }
      .card {
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        background: var(--surface);
        padding: 16px;
        display: grid;
        gap: 10px;
      }
      .card.red {
        border-color: var(--red-border);
      }
      .card.amber {
        border-color: var(--amber-border);
      }
      .card.blue {
        border-color: var(--accent-border);
      }
      .head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 10px;
      }
      .head ::slotted([slot="title"]) {
        font-size: var(--bk-font-base);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
      }
      .summary ::slotted([slot="summary"]) {
        color: var(--text-2);
        line-height: 1.6;
      }
      .meta ::slotted([slot="meta"]) {
        color: var(--text-3);
        font-size: var(--bk-font-sm);
      }
      .raw ::slotted([slot="raw"]) {
        margin: 0;
      }
    `;
  }

  attributeChangedCallback() {
    if (this.isConnected) {this._render();}
  }

  _render() {
    const tone = this.getAttribute('tone') || 'blue';
    this._shadow.innerHTML = `
      <article class="card ${this.esc(tone)}">
        <div class="head">
          <slot name="title"></slot>
          <slot name="meta"></slot>
        </div>
        <div class="summary">
          <slot name="summary"></slot>
        </div>
        <div class="raw">
          <slot name="raw"></slot>
        </div>
      </article>
    `;
  }
}

customElements.define('platform-evidence-card', PlatformEvidenceCard);
export { PlatformEvidenceCard };
