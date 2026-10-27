import { PlatformElement } from '../platform-base.js';

const SAFE_LINK_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);

class PlatformMarkdown extends PlatformElement {
  static get styles() {
    return `
      :host {
        display: block;
      }
      .md {
        font-size: var(--bk-font-md, 14px);
        color: var(--text-2);
        line-height: 1.6;
        word-wrap: break-word;
        overflow-wrap: break-word;
      }
      .md h1 {
        font-size: 1.5em;
        font-weight: 700;
        color: var(--text-1);
        margin: 1em 0 0.5em;
      }
      .md h1:first-child { margin-top: 0; }
      .md h2 {
        font-size: 1.25em;
        font-weight: 600;
        color: var(--text-1);
        margin: 1em 0 0.5em;
      }
      .md h3 {
        font-size: 1.1em;
        font-weight: 600;
        color: var(--text-1);
        margin: 1em 0 0.4em;
      }
      .md p {
        margin: 0.5em 0;
      }
      .md p:first-child { margin-top: 0; }
      .md p:last-child { margin-bottom: 0; }
      .md code {
        font-family: var(--bk-mono, monospace);
        font-size: 0.9em;
        background: var(--hover);
        padding: 2px 5px;
        border-radius: var(--r-sm);
      }
      .md pre {
        background: var(--content-bg);
        border: 1px solid var(--border);
        border-radius: var(--r-md);
        padding: 12px 16px;
        overflow-x: auto;
        margin: 0.75em 0;
      }
      .md pre code {
        background: none;
        padding: 0;
      }
      .md ul, .md ol {
        padding-left: 1.5em;
        margin: 0.5em 0;
      }
      .md li {
        margin: 0.25em 0;
      }
      .md blockquote {
        border-left: 3px solid var(--accent-border);
        padding-left: 12px;
        color: var(--text-3);
        margin: 0.75em 0;
      }
      .md strong {
        color: var(--text-1);
        font-weight: 600;
      }
      .md hr {
        border: none;
        border-top: 1px solid var(--border);
        margin: 1em 0;
      }
      .md a {
        color: var(--accent);
        text-decoration: none;
      }
      .md a:hover {
        text-decoration: underline;
      }
    `;
  }

  _render() {
    const c = this._config;
    const content = c.content || '';
    const html = this._parseMarkdown(content);
    this._shadow.innerHTML = `<div class="md">${html}</div>`;
  }

  /** Minimal Markdown-to-HTML converter (no external deps). */
  _parseMarkdown(src) {
    let html = this.esc(src);

    // Code blocks (``` ... ```)
    html = html.replaceAll(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
      const trimmedCode = code.replace(/^\n/, '').replace(/\n$/, '');
      return `<pre><code>${trimmedCode}</code></pre>`;
    });

    // Inline code
    html = html.replaceAll(/`([^`\n]+)`/g, '<code>$1</code>');

    // Headings
    html = html.replaceAll(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replaceAll(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replaceAll(/^# (.+)$/gm, '<h1>$1</h1>');

    // Horizontal rule
    html = html.replaceAll(/^---$/gm, '<hr>');

    // Bold and italic
    html = html.replaceAll(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replaceAll(/\*(.+?)\*/g, '<em>$1</em>');

    // Links [text](url)
    html = this._replaceMarkdownLinks(html);

    // Blockquotes
    html = html.replaceAll(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

    // Unordered lists (simple, single-level)
    html = html.replaceAll(/(^[-*] .+\n?)+/gm, match => {
      const items = match.trim().split('\n')
        .map(l => `<li>${l.replace(/^[-*] /, '')}</li>`).join('');
      return `<ul>${items}</ul>`;
    });

    // Paragraphs: wrap remaining loose lines
    html = html.replaceAll(/^(?!<[a-z])((?!<\/)[^\n]+)$/gm, '<p>$1</p>');

    // Clean up double paragraph wraps from blockquote/list
    html = html.replaceAll(/<p>(<(?:ul|ol|h[1-3]|hr|pre|blockquote))/g, '$1');
    html = html.replaceAll(/(<\/(?:ul|ol|h[1-3]|hr|pre|blockquote)>)<\/p>/g, '$1');

    return html;
  }

  _replaceMarkdownLinks(html) {
    const source = String(html || '');
    let result = '';
    let cursor = 0;

    while (cursor < source.length) {
      const textStart = source.indexOf('[', cursor);
      if (textStart === -1) {
        result += source.slice(cursor);
        break;
      }

      const textEnd = source.indexOf(']', textStart + 1);
      const urlStart = textEnd >= 0 && source[textEnd + 1] === '(' ? textEnd + 2 : -1;
      if (textEnd === -1 || urlStart === -1) {
        result += source.slice(cursor);
        break;
      }

      const urlEnd = source.indexOf(')', urlStart);
      if (urlEnd === -1) {
        result += source.slice(cursor);
        break;
      }

      const label = source.slice(textStart + 1, textEnd).trim();
      const href = source.slice(urlStart, urlEnd).trim();
      result += source.slice(cursor, textStart);

      if (label && href && !this._isUnsafeLinkHref(href)) {
        result += `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
      } else {
        result += source.slice(textStart, urlEnd + 1);
      }
      cursor = urlEnd + 1;
    }

    return result;
  }

  _isUnsafeLinkHref(href) {
    const normalized = String(href || '').trim();
    if (!normalized) {
      return true;
    }
    if (normalized.startsWith('//')) {
      return true;
    }
    if (normalized.startsWith('/') || normalized.startsWith('#') || normalized.startsWith('?')) {
      return false;
    }

    const schemeSeparator = normalized.indexOf(':');
    if (schemeSeparator <= 0) {
      return false;
    }

    const scheme = normalized.slice(0, schemeSeparator).trim().toLowerCase();
    return !SAFE_LINK_SCHEMES.has(scheme);
  }
}

customElements.define('platform-markdown', PlatformMarkdown);
export { PlatformMarkdown };
