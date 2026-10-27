/**
 * PlatformElement — Base class for all platform-* Web Components.
 *
 * Provides:
 *  - Shadow DOM with adopted design-token stylesheet
 *  - config property (set once, triggers render)
 *  - HTML escaping helper
 *  - Typed CustomEvent emitter
 */

let _tokenSheet = new CSSStyleSheet();

function decodeDataUrlText(dataUrl) {
  const value = String(dataUrl || '');
  const commaIndex = value.indexOf(',');
  if (!value.startsWith('data:') || commaIndex < 0) {
    return '';
  }

  const meta = value.slice(5, commaIndex).toLowerCase();
  const payload = value.slice(commaIndex + 1);
  if (meta.includes(';base64')) {
    const binary = globalThis.atob(payload);
    const bytes = Uint8Array.from(binary, (char) => char.codePointAt(0) || 0);
    return new globalThis.TextDecoder().decode(bytes);
  }
  return decodeURIComponent(payload.replaceAll('+', '%20'));
}

async function loadTokenCssText() {
  const tokenUrl = new URL('./tokens.css', import.meta.url);
  if (tokenUrl.protocol === 'data:') {
    return decodeDataUrlText(tokenUrl.href);
  }

  const resp = await fetch(tokenUrl);
  return await resp.text();
}

try {
  const text = await loadTokenCssText();
  _tokenSheet.replaceSync(text);
} catch {
  _tokenSheet.replaceSync('');
}

// Pre-fetch tokens on module load
const tokenSheetReady = Promise.resolve(_tokenSheet);

export class PlatformElement extends HTMLElement {
  /** Override in subclass to return component-specific CSS string */
  static get styles() { return ''; }

  constructor() {
    super();
    this._config = {};
    this._componentSheet = null;
    this._shadow = this.attachShadow({ mode: 'open' });
  }

  async connectedCallback() {
    // Recover pre-upgrade 'config' own-property (set before element was defined/upgraded)
    const ownDesc = Object.getOwnPropertyDescriptor(this, 'config');
    if (ownDesc && 'value' in ownDesc) {
      const val = ownDesc.value;
      delete this.config;
      this._config = val || {};
    }

    const tokenSheet = await tokenSheetReady;
    if (!this._componentSheet) {
      this._componentSheet = new CSSStyleSheet();
      this._componentSheet.replaceSync(this.constructor.styles);
    }
    this._shadow.adoptedStyleSheets = [tokenSheet, this._componentSheet];
    this._render();
  }

  get config() { return this._config; }

  set config(val) {
    this._config = val || {};
    if (this.isConnected) {this._render();}
  }

  /** Safe HTML escaping */
  static esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  /** Shorthand instance method */
  esc(s) { return PlatformElement.esc(s); }

  t(key, fallback = '', params = undefined) {
    if (typeof globalThis.uiT === 'function') {
      return globalThis.uiT(key, fallback, params);
    }
    return String(fallback ?? key ?? '');
  }

  /** Emit a typed CustomEvent that bubbles and crosses shadow boundaries */
  emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, {
      detail,
      bubbles: true,
      composed: true,
    }));
  }

  /** Override in subclass — called when config changes or component connects */
  _render() {
    this._shadow.innerHTML = '<slot></slot>';
  }

  /** Dot-path getter */
  static getPath(obj, path) {
    if (!path) {return obj;}
    return path.split('.').reduce((o, k) => (o !== null && o !== undefined ? o[k] : undefined), obj);
  }

  /** Dot-path setter */
  static setPath(obj, path, value) {
    const keys = path.split('.');
    let cur = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      if (cur[keys[i]] === null || cur[keys[i]] === undefined) {cur[keys[i]] = {};}
      cur = cur[keys[i]];
    }
    cur[keys[keys.length - 1]] = value;
  }

  /** Deep clone helper */
  static clone(value) {
    const safeValue = value === null || value === undefined ? {} : value;
    try {
      return globalThis.structuredClone(safeValue);
    }
    catch { return value; }
  }
}
