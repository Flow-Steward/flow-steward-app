import { PlatformElement } from '../platform-base.js';

class PlatformReviewWorkspace extends PlatformElement {
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
        padding: 20px;
        display: grid;
        gap: 16px;
      }
      .shell.amber {
        border-color: var(--amber-border);
        background: var(--surface);
      }
      .shell.red {
        border-color: var(--red-border);
        background: var(--surface);
      }
      .shell.blue {
        border-color: var(--accent-border);
      }
      .header {
        display: grid;
        gap: 8px;
      }
      .header ::slotted(*) {
        margin: 0;
      }
      .body {
        display: grid;
        gap: 14px;
      }
    `;
  }

  attributeChangedCallback() {
    if (this.isConnected) {this._render();}
  }

  _render() {
    const tone = this.getAttribute('tone') || 'amber';
    this._shadow.innerHTML = `
      <section class="shell ${this.esc(tone)}">
        <div class="header">
          <slot name="header"></slot>
        </div>
        <div class="body">
          <slot></slot>
        </div>
      </section>
    `;
  }
}

customElements.define('platform-review-workspace', PlatformReviewWorkspace);
export { PlatformReviewWorkspace };
