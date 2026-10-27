import { PlatformElement } from '../platform-base.js';

class PlatformSection extends PlatformElement {
  static get styles() {
    return `
      :host {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        overflow: hidden;
        display: block;
      }
      :host(.plain) {
        background: transparent;
        border: none;
        border-radius: 0;
      }
      :host(.inset) {
        background: var(--content-bg);
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 18px;
        cursor: default;
        user-select: none;
      }
      :host([collapsible]) .header {
        cursor: pointer;
      }
      :host([collapsible]) .header:hover {
        background: var(--hover);
      }
      .title {
        font-size: var(--bk-font-base);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
      }
      .desc {
        font-size: var(--bk-font-sm);
        color: var(--text-3);
        margin: 2px 0 0;
      }
      .chevron {
        width: 16px;
        height: 16px;
        color: var(--text-3);
        transition: transform 0.15s;
        flex-shrink: 0;
      }
      :host([collapsed]) .chevron {
        transform: rotate(-90deg);
      }
      .body {
        padding: 0 18px 18px;
      }
      :host([collapsed]) .body {
        display: none;
      }
    `;
  }

  static get observedAttributes() {
    return ['collapsible', 'collapsed'];
  }

  set config(val) {
    this._config = val || {};
    const c = this._config;

    // Apply variant class
    this.classList.remove('plain', 'inset');
    if (c.variant === 'plain' || c.variant === 'inset') {
      this.classList.add(c.variant);
    }

    // Apply collapsible attribute
    if (c.collapsible) {
      this.setAttribute('collapsible', '');
      if (c.default_open === false) {
        this.setAttribute('collapsed', '');
      } else {
        this.removeAttribute('collapsed');
      }
    } else {
      this.removeAttribute('collapsible');
      this.removeAttribute('collapsed');
    }

    if (this.isConnected) {this._render();}
  }

  get config() { return this._config; }

  _toggle() {
    if (!this._config.collapsible) {return;}
    if (this.hasAttribute('collapsed')) {
      this.removeAttribute('collapsed');
    } else {
      this.setAttribute('collapsed', '');
    }
  }

  _render() {
    const c = this._config;
    const title = c.title ? `<h3 class="title">${this.esc(c.title)}</h3>` : '';
    const desc = c.description ? `<p class="desc">${this.esc(c.description)}</p>` : '';
    const hasHeader = c.title || c.description;

    const chevronSvg = c.collapsible
      ? `<svg class="chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
           <polyline points="4 6 8 10 12 6"/>
         </svg>`
      : '';

    this._shadow.innerHTML = `
      ${hasHeader ? `<div class="header">
        <div class="header-text">${title}${desc}</div>
        ${chevronSvg}
      </div>` : ''}
      <div class="body">
        <slot></slot>
      </div>
    `;

    if (hasHeader && c.collapsible) {
      this._shadow.querySelector('.header')
        .addEventListener('click', () => this._toggle());
    }
  }
}

customElements.define('platform-section', PlatformSection);
export { PlatformSection };
