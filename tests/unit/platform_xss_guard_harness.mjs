/**
 * Node harness for platform UI XSS / action-allowlist guards.
 *
 * Loaded by tests/unit/test_platform_xss_guards.py via `node` subprocess.
 * Imports the real guard helpers from the asset modules and asserts behaviour.
 * Prints "OK" on success; throws (non-zero exit) on any failed assertion.
 *
 * Run standalone:  node tests/unit/platform_xss_guard_harness.mjs
 */
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');

// jsdom-free: stub the browser globals the asset modules touch at import time
// (custom elements registry, base HTMLElement, constructable stylesheets).
const _registry = new Map();
globalThis.customElements = globalThis.customElements || {
  define(name, ctor) { _registry.set(name, ctor); },
  get(name) { return _registry.get(name); },
};
globalThis.HTMLElement = globalThis.HTMLElement || class {};
globalThis.document = globalThis.document || {
  createElement: () => ({ appendChild() {}, addEventListener() {}, set config(_v) {} }),
  adoptedStyleSheets: [],
};
globalThis.CSSStyleSheet = globalThis.CSSStyleSheet || class { replaceSync() {} replace() {} };

const gridUrl = pathToFileURL(
  path.join(ROOT, 'assets', 'platform-kit', 'components', 'platform-grid.js'),
).href;
const rendererUrl = pathToFileURL(
  path.join(ROOT, 'assets', 'platform-renderer.js'),
).href;
const actionBarUrl = pathToFileURL(
  path.join(ROOT, 'assets', 'platform-kit', 'components', 'platform-action-bar.js'),
).href;

const { safeLinkHref, isUnsafeLinkHref } = await import(gridUrl);
const { filterDisplaySafeComponents, isExecutableComponentType } = await import(rendererUrl);
const { safeActionBarClassToken } = await import(actionBarUrl);

/* ── CAND-035: grid link-cell safe-href allowlist ── */

// NEGATIVE: dangerous schemes are neutralized to '#'.
for (const bad of [
  'javascript:alert(1)',
  'JavaScript:alert(1)',
  '  javascript:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'data:text/html;base64,PHNjcmlwdD4=',
  'vbscript:msgbox(1)',
  'file:///etc/passwd',
  '//evil.example/cookie',
  '',
  '   ',
]) {
  assert.equal(isUnsafeLinkHref(bad), true, `expected unsafe: ${JSON.stringify(bad)}`);
  assert.equal(safeLinkHref(bad), '#', `expected neutralized '#': ${JSON.stringify(bad)}`);
}

// POSITIVE: safe schemes and relative URLs pass through unchanged.
for (const ok of [
  'https://example.com/page?q=1',
  'http://example.com',
  'mailto:ops@example.com',
  'tel:+15551234',
  '/projects/42',
  '#section',
  '?filter=open',
  'reports/2026',                 // bare relative path, no scheme
  'https://example.com/a:b',      // colon after a slash is not a scheme
]) {
  assert.equal(isUnsafeLinkHref(ok), false, `expected safe: ${JSON.stringify(ok)}`);
  assert.equal(safeLinkHref(ok), ok, `expected unchanged: ${JSON.stringify(ok)}`);
}

/* ── CAND-066: detail-payload executable-component allowlist ── */

assert.equal(isExecutableComponentType('action_bar'), true);
assert.equal(isExecutableComponentType('form'), true);
assert.equal(isExecutableComponentType('confirm_action'), true);
assert.equal(isExecutableComponentType('filter_bar'), true);
assert.equal(isExecutableComponentType('drawer_detail'), true);
assert.equal(isExecutableComponentType('markdown'), false);
assert.equal(isExecutableComponentType('key_value'), false);
assert.equal(isExecutableComponentType('grid'), false);

// NEGATIVE: malicious extension detail payload mixing executable + display
// components. Executable ones (incl. nested in a container) are dropped.
const malicious = [
  { type: 'markdown', component_id: 'md', body: 'hi' },
  { type: 'action_bar', component_id: 'evil_bar', actions: [{ action_id: 'wipe_db', label: 'x' }] },
  { type: 'confirm_action', component_id: 'evil_confirm', action_id: 'exfiltrate' },
  {
    type: 'section',
    component_id: 'wrap',
    components: [
      { type: 'form', component_id: 'evil_form' },
      { type: 'key_value', component_id: 'kv_ok' },
    ],
  },
];
const filtered = filterDisplaySafeComponents(malicious);
const topTypes = filtered.map((c) => c.type);
assert.deepEqual(topTypes, ['markdown', 'section'], 'executable top-level components must be dropped');
const section = filtered.find((c) => c.type === 'section');
assert.deepEqual(
  section.components.map((c) => c.type),
  ['key_value'],
  'nested executable components must be dropped, display ones kept',
);

// POSITIVE: an all-display payload survives intact (allowlisted components work).
const safePayload = [
  { type: 'markdown', component_id: 'a', body: 'x' },
  { type: 'grid', component_id: 'g', columns: [] },
  { type: 'key_value', component_id: 'k' },
];
assert.deepEqual(
  filterDisplaySafeComponents(safePayload).map((c) => c.component_id),
  ['a', 'g', 'k'],
  'display-safe payload must pass through unchanged',
);

/* ── Action bar class-token allowlists ── */

const variants = new Set(['primary', 'danger', 'ghost']);
const sizes = new Set(['sm', 'lg']);
assert.equal(safeActionBarClassToken('primary', variants), 'primary');
assert.equal(safeActionBarClassToken('LG', sizes), 'lg');
assert.equal(safeActionBarClassToken('primary" autofocus onclick="alert(1)', variants), '');
assert.equal(safeActionBarClassToken('sm onmouseover=alert(1)', sizes), '');

console.log('OK');
