import { PlatformElement } from '../platform-base.js';

class PlatformConfirm extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: none;
        position: fixed;
        inset: 0;
        z-index: 9999;
      }
      :host(.open) {
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.45);
        animation: fadeIn 0.15s ease;
      }
      .dialog {
        position: relative;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        box-shadow: var(--shadow-lg);
        min-width: 340px;
        max-width: 480px;
        width: 100%;
        animation: scaleIn 0.15s ease;
        overflow: hidden;
      }
      .dialog-header {
        padding: 18px 20px 0;
      }
      .dialog-title {
        font-size: var(--bk-font-lg);
        font-weight: 600;
        color: var(--text-1);
        margin: 0;
      }
      .dialog-body {
        padding: 12px 20px 20px;
        font-size: var(--bk-font-base);
        color: var(--text-2);
        line-height: 1.5;
      }
      .dialog-footer {
        display: flex;
        justify-content: flex-end;
        gap: var(--bk-gap-sm);
        padding: 0 20px 18px;
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
      @keyframes fadeIn {
        from { opacity: 0; }
        to   { opacity: 1; }
      }
      @keyframes scaleIn {
        from { opacity: 0; transform: scale(0.95); }
        to   { opacity: 1; transform: scale(1); }
      }
    `;
  }

  constructor() {
    super();
    this._resolve = null;
  }

  /**
   * Show the confirmation dialog.
   * @returns {Promise<boolean>} true if confirmed, false if cancelled
   */
  show() {
    this.classList.add('open');
    this._render();
    return new Promise(resolve => {
      this._resolve = resolve;
    });
  }

  close(confirmed) {
    this.classList.remove('open');
    if (this._resolve) {
      this._resolve(confirmed);
      this._resolve = null;
    }
    this.emit(confirmed ? 'platform-confirm' : 'platform-cancel');
  }

  _render() {
    const c = this._config;
    const title = c.title || 'Confirm';
    const message = c.message || 'Are you sure?';
    const confirmLabel = c.confirm_label || 'Confirm';
    const cancelLabel = c.cancel_label || 'Cancel';
    const variant = c.variant || 'primary';

    this._shadow.innerHTML = `
      <div class="overlay"></div>
      <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
        <div class="dialog-header">
          <h3 class="dialog-title" id="confirm-title">${this.esc(title)}</h3>
        </div>
        <div class="dialog-body">${this.esc(message)}</div>
        <div class="dialog-footer">
          <button class="bk-btn" data-role="cancel">${this.esc(cancelLabel)}</button>
          <button class="bk-btn ${this.esc(variant)}" data-role="confirm">${this.esc(confirmLabel)}</button>
        </div>
      </div>
    `;

    this._shadow.querySelector('.overlay')
      .addEventListener('click', () => this.close(false));
    this._shadow.querySelector('[data-role="cancel"]')
      .addEventListener('click', () => this.close(false));
    this._shadow.querySelector('[data-role="confirm"]')
      .addEventListener('click', () => this.close(true));

    // Trap Escape key
    this._keyHandler = (e) => {
      if (e.key === 'Escape') {this.close(false);}
    };
    document.addEventListener('keydown', this._keyHandler);
  }

  disconnectedCallback() {
    if (this._keyHandler) {
      document.removeEventListener('keydown', this._keyHandler);
    }
  }
}

customElements.define('platform-confirm', PlatformConfirm);
export { PlatformConfirm };
