import { PlatformElement } from '../platform-base.js';

const ACTION_BAR_ALIGNMENTS = new Set(['left', 'right', 'between']);
const ACTION_BAR_VARIANTS = new Set(['primary', 'danger', 'ghost']);
const ACTION_BAR_SIZES = new Set(['sm', 'lg']);

function safeActionBarClassToken(value, allowedTokens) {
  const normalized = String(value || '').trim().toLowerCase();
  return allowedTokens.has(normalized) ? normalized : '';
}

class PlatformActionBar extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .bar {
        display: flex;
        align-items: center;
        gap: var(--bk-gap-sm, 8px);
        flex-wrap: wrap;
      }
      .bar.align-left {
        justify-content: flex-start;
      }
      .bar.align-right {
        justify-content: flex-end;
      }
      .bar.align-between {
        justify-content: space-between;
      }
      .bk-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 7px 14px;
        font-size: var(--bk-font-sm);
        font-weight: 500;
        font-family: inherit;
        border-radius: var(--r-md);
        border: 1px solid var(--border);
        background: var(--surface);
        color: var(--text-1);
        cursor: pointer;
        transition: background 0.12s, border-color 0.12s;
        user-select: none;
        line-height: 1.4;
      }
      .bk-btn:hover {
        background: var(--hover);
      }
      .bk-btn.primary {
        background: var(--accent);
        color: #fff;
        border-color: var(--accent);
      }
      .bk-btn.primary:hover {
        filter: brightness(1.1);
      }
      .bk-btn.danger {
        background: var(--red);
        color: #fff;
        border-color: var(--red);
      }
      .bk-btn.danger:hover {
        filter: brightness(1.1);
      }
      .bk-btn.ghost {
        background: transparent;
        border-color: transparent;
        color: var(--text-2);
      }
      .bk-btn.ghost:hover {
        background: var(--hover);
        color: var(--text-1);
      }
      .bk-btn.sm {
        padding: 4px 10px;
        font-size: var(--bk-font-xs, 12px);
      }
      .bk-btn.lg {
        padding: 10px 20px;
        font-size: var(--bk-font-base);
      }
      .btn-icon {
        width: 14px;
        height: 14px;
        flex-shrink: 0;
      }
    `;
  }

  _render() {
    const c = this._config;
    const align = safeActionBarClassToken(c.align, ACTION_BAR_ALIGNMENTS) || 'left';
    const actions = c.actions || [];

    const buttons = actions.map(a => {
      const variant = safeActionBarClassToken(a.variant, ACTION_BAR_VARIANTS);
      const size = safeActionBarClassToken(a.size, ACTION_BAR_SIZES);
      const cls = ['bk-btn', variant, size].filter(Boolean).join(' ');
      const icon = a.icon
        ? `<svg class="btn-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#icon-${this.esc(a.icon)}"/></svg>`
        : '';
      return `<button class="${this.esc(cls)}" data-action="${this.esc(a.action_id)}">${icon}${this.esc(a.label)}</button>`;
    }).join('');

    this._shadow.innerHTML = `<div class="bar align-${this.esc(align)}">${buttons}</div>`;

    this._shadow.querySelectorAll('.bk-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.emit('platform-action', { action_id: btn.dataset.action });
      });
    });
  }
}

customElements.define('platform-action-bar', PlatformActionBar);
export { PlatformActionBar, safeActionBarClassToken };
