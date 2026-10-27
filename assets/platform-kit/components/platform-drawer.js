import { PlatformElement } from '../platform-base.js';

class PlatformDrawer extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: none;
        position: fixed;
        inset: 0;
        z-index: 9000;
      }
      :host(.open) {
        display: block;
      }
      .overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.35);
        animation: fadeIn 0.2s ease;
      }
      .drawer {
        position: fixed;
        top: 0;
        right: 0;
        bottom: 0;
        width: min(520px, 90vw);
        background: var(--surface);
        border-left: 1px solid var(--border);
        box-shadow: var(--shadow-lg);
        display: flex;
        flex-direction: column;
        animation: slideIn 0.2s ease;
      }
      .drawer-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px 20px;
        border-bottom: 1px solid var(--border);
        flex-shrink: 0;
      }
      .drawer-title {
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .close-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        border: none;
        background: transparent;
        color: var(--text-3);
        cursor: pointer;
        border-radius: var(--r-sm);
        flex-shrink: 0;
        transition: background 0.12s, color 0.12s;
      }
      .close-btn:hover {
        background: var(--hover);
        color: var(--text-1);
      }
      .close-btn svg {
        width: 16px;
        height: 16px;
      }
      .drawer-body {
        flex: 1;
        overflow-y: auto;
        padding: 20px;
      }
      .drawer-footer {
        border-top: 1px solid var(--border);
        padding: 12px 20px;
        flex-shrink: 0;
      }
      .drawer-footer:empty {
        display: none;
      }
      @keyframes fadeIn {
        from { opacity: 0; }
        to   { opacity: 1; }
      }
      @keyframes slideIn {
        from { transform: translateX(100%); }
        to   { transform: translateX(0); }
      }
      @keyframes slideOut {
        from { transform: translateX(0); }
        to   { transform: translateX(100%); }
      }
    `;
  }

  constructor() {
    super();
    this._title = '';
    this._contentHtml = '';
    this._keyHandler = null;
  }

  async connectedCallback() {
    await super.connectedCallback();
    // Add once for the element's lifetime; guard so Escape only fires when open.
    this._keyHandler = (e) => {
      if (e.key === 'Escape' && this.classList.contains('open')) {this.close();}
    };
    document.addEventListener('keydown', this._keyHandler);
  }

  /**
   * Open the drawer with a title and HTML content.
   * @param {string} title
   * @param {string} contentHtml - trusted HTML to render in the body
   */
  open(title, contentHtml) {
    this._title = title || this._config.title || '';
    this._contentHtml = contentHtml || '';
    this.classList.add('open');
    this._render();
  }

  /** Close the drawer. */
  close() {
    this.classList.remove('open');
    this.emit('platform-close');
  }

  _render() {
    const title = this._title || this._config.title || '';

    this._shadow.innerHTML = `
      <div class="overlay"></div>
      <div class="drawer" role="dialog" aria-modal="true" aria-label="${this.esc(title)}">
        <div class="drawer-header">
          <h3 class="drawer-title">${this.esc(title)}</h3>
          <button class="close-btn" aria-label="Close">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="4" x2="12" y2="12"/>
              <line x1="12" y1="4" x2="4" y2="12"/>
            </svg>
          </button>
        </div>
        <div class="drawer-body">${this._contentHtml}</div>
        <div class="drawer-footer"><slot name="footer"></slot></div>
      </div>
    `;

    this._shadow.querySelector('.overlay')
      .addEventListener('click', () => this.close());
    this._shadow.querySelector('.close-btn')
      .addEventListener('click', () => this.close());

  }

  disconnectedCallback() {
    if (this._keyHandler) {
      document.removeEventListener('keydown', this._keyHandler);
      this._keyHandler = null;
    }
  }
}

customElements.define('platform-drawer', PlatformDrawer);
export { PlatformDrawer };
