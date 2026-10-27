import { PlatformElement } from '../platform-base.js';

class PlatformStack extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: flex;
        flex-direction: column;
        gap: var(--bk-gap-lg);
      }
    `;
  }

  _render() {
    this._shadow.innerHTML = '<slot></slot>';
  }
}

customElements.define('platform-stack', PlatformStack);
export { PlatformStack };
