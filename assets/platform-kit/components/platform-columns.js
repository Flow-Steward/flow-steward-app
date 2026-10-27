import { PlatformElement } from '../platform-base.js';

class PlatformColumns extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: grid;
        gap: var(--bk-gap-lg);
        grid-template-columns: 1fr;
      }
      :host(.columns--2) {
        grid-template-columns: 1fr 1fr;
      }
      @media (max-width: 640px) {
        :host(.columns--2) {
          grid-template-columns: 1fr;
        }
      }
    `;
  }

  set config(val) {
    this._config = val || {};
    this.classList.remove('columns--2');
    if (this._config.columns === 2) {
      this.classList.add('columns--2');
    }
    if (this.isConnected) {this._render();}
  }

  get config() { return this._config; }

  _render() {
    this._shadow.innerHTML = '<slot></slot>';
  }
}

customElements.define('platform-columns', PlatformColumns);
export { PlatformColumns };
