import { PlatformElement } from '../platform-base.js';

class PlatformFilterBar extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .filter-bar {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--bk-gap-sm);
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        font-size: var(--bk-font-sm);
        font-weight: 500;
        color: var(--accent);
        background: var(--accent-soft);
        border: 1px solid var(--accent-border);
        border-radius: 999px;
        white-space: nowrap;
        user-select: none;
        line-height: 1.4;
      }
      .chip-label {
        max-width: 200px;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .chip-remove {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 14px;
        height: 14px;
        border: none;
        background: transparent;
        color: var(--accent);
        cursor: pointer;
        padding: 0;
        border-radius: 50%;
        flex-shrink: 0;
        transition: background 0.12s;
      }
      .chip-remove:hover {
        background: var(--accent-border);
      }
      .chip-remove svg {
        width: 10px;
        height: 10px;
      }
      .add-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px;
        font-size: var(--bk-font-sm);
        font-weight: 500;
        font-family: inherit;
        color: var(--text-3);
        background: transparent;
        border: 1px dashed var(--border);
        border-radius: 999px;
        cursor: pointer;
        transition: background 0.12s, color 0.12s, border-color 0.12s;
        user-select: none;
        line-height: 1.4;
      }
      .add-btn:hover {
        background: var(--hover);
        color: var(--text-1);
        border-color: var(--text-3);
      }
      .add-btn svg {
        width: 12px;
        height: 12px;
      }
      .empty {
        font-size: var(--bk-font-sm);
        color: var(--text-3);
      }
    `;
  }

  _render() {
    const c = this._config;
    const filters = c.filters || [];

    const chips = filters.map((f, i) => `
      <span class="chip">
        <span class="chip-label" title="${this.esc(f.label + ': ' + f.value)}">${this.esc(f.label)}: ${this.esc(f.value)}</span>
        <button class="chip-remove" data-index="${i}" aria-label="Remove filter">
          <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5"
               stroke-linecap="round" stroke-linejoin="round">
            <line x1="2" y1="2" x2="8" y2="8"/>
            <line x1="8" y1="2" x2="2" y2="8"/>
          </svg>
        </button>
      </span>
    `).join('');

    const addBtn = `
      <button class="add-btn" aria-label="Add filter">
        <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" stroke-linejoin="round">
          <line x1="6" y1="2" x2="6" y2="10"/>
          <line x1="2" y1="6" x2="10" y2="6"/>
        </svg>
        Filter
      </button>
    `;

    this._shadow.innerHTML = `
      <div class="filter-bar">
        ${chips}
        ${addBtn}
      </div>
    `;

    this._shadow.querySelectorAll('.chip-remove').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const index = Number.parseInt(btn.dataset.index, 10);
        this.emit('platform-filter-remove', { index });
      });
    });

    this._shadow.querySelector('.add-btn')
      .addEventListener('click', () => {
        this.emit('platform-filter-add');
      });
  }
}

customElements.define('platform-filter-bar', PlatformFilterBar);
export { PlatformFilterBar };
