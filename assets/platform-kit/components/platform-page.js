import { PlatformElement } from '../platform-base.js';

class PlatformPage extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .tab-bar {
        display: flex;
        gap: 0;
        border-bottom: 1px solid var(--border);
        margin-bottom: var(--bk-gap-lg);
        overflow-x: auto;
        scrollbar-width: none;
      }
      .tab-bar::-webkit-scrollbar {
        display: none;
      }
      .tab {
        display: inline-flex;
        align-items: center;
        padding: 10px 16px;
        font-size: var(--bk-font-sm);
        font-weight: 500;
        font-family: inherit;
        color: var(--text-3);
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        cursor: pointer;
        white-space: nowrap;
        transition: color 0.12s, border-color 0.12s;
        user-select: none;
      }
      .tab:hover {
        color: var(--text-1);
        background: var(--hover);
      }
      .tab.active {
        color: var(--accent);
        border-bottom-color: var(--accent);
      }
      .description {
        font-size: var(--bk-font-sm);
        color: var(--text-3);
        margin: 0 0 var(--bk-gap-lg);
        line-height: 1.5;
      }
      .content {
        min-height: 0;
      }
    `;
  }

  _render() {
    const c = this._config;
    const pages = c.pages || [];
    const currentId = c.current_page_id || c.active_page_id || pages[0]?.page_id || '';

    const currentPage = pages.find(p => p.page_id === currentId);

    const tabs = pages.length > 1
      ? `<div class="tab-bar" role="tablist">
          ${pages.map(p => `
            <button class="tab${p.page_id === currentId ? ' active' : ''}"
                    role="tab"
                    aria-selected="${p.page_id === currentId}"
                    data-page="${this.esc(p.page_id)}">
              ${this.esc(p.title)}
            </button>
          `).join('')}
        </div>`
      : '';

    const desc = currentPage?.description
      ? `<p class="description">${this.esc(currentPage.description)}</p>`
      : '';

    this._shadow.innerHTML = `
      ${tabs}
      ${desc}
      <div class="content" role="tabpanel">
        <slot></slot>
      </div>
    `;

    this._shadow.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const pageId = tab.dataset.page;
        if (pageId !== currentId) {
          this.emit('platform-navigate', { page_id: pageId });
        }
      });
    });
  }
}

if (!customElements.get('platform-page')) {
  customElements.define('platform-page', PlatformPage);
}
export { PlatformPage };
