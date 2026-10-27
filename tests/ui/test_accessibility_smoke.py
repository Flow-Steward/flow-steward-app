"""
UI accessibility smoke checks for key pages.

Scope:
- Confirm key shell landmarks exist (`main`, navigation, heading).
- Catch visible interactive controls that do not expose an accessible name.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


def _routes_for_role(active_test_role: str) -> tuple[str, ...]:
    routes: list[str] = [
        "/dashboard",
        "/projects",
        "/jobs",
        "/settings/account",
    ]
    if active_test_role in {"admin", "operator"}:
        routes.append("/registry/workflows")
    if active_test_role == "admin":
        routes.append("/settings")
    return tuple(routes)


def _is_login_page(content: str) -> bool:
    body = str(content or "").lower()
    return "login" in body and ("password" in body or "sign in" in body or "username" in body)


def _accessibility_report(page) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const interactiveSelector = [
              'button',
              'a[href]',
              'input:not([type="hidden"])',
              'select',
              'textarea',
              '[role="button"]',
              '[role="link"]',
              '[role="tab"]',
              '[role="checkbox"]',
              '[role="radio"]',
              '[role="switch"]',
              '[role="combobox"]',
              '[role="textbox"]',
              '[role="menuitem"]'
            ].join(', ');

            const isVisible = (el) => {
              if (!el || !(el instanceof Element)) return false;
              if (el.hasAttribute('hidden')) return false;
              if (el.getAttribute('aria-hidden') === 'true') return false;
              if (el.getAttribute('tabindex') === '-1') return false;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              const rect = el.getBoundingClientRect();
              return rect.width >= 1 && rect.height >= 1;
            };

            const elementSummary = (el) => {
              const tag = String(el.tagName || '').toLowerCase();
              const role = String(el.getAttribute('role') || '').trim();
              const elId = String(el.id || '').trim();
              const cls = String(el.className || '').trim();
              return {
                tag,
                role,
                id: elId,
                className: cls.slice(0, 120),
              };
            };

            const nameFromLabelledBy = (el) => {
              const ids = String(el.getAttribute('aria-labelledby') || '')
                .split(/\\s+/)
                .map((v) => v.trim())
                .filter(Boolean);
              if (!ids.length) return '';
              const text = ids
                .map((id) => document.getElementById(id))
                .filter(Boolean)
                .map((node) => String(node.textContent || '').trim())
                .join(' ')
                .trim();
              return text;
            };

            const accessibleName = (el) => {
              const ariaLabel = String(el.getAttribute('aria-label') || '').trim();
              if (ariaLabel) return ariaLabel;

              const labelledBy = nameFromLabelledBy(el);
              if (labelledBy) return labelledBy;

              if ('labels' in el && el.labels && el.labels.length > 0) {
                const labelText = Array.from(el.labels)
                  .map((label) => String(label.textContent || '').trim())
                  .join(' ')
                  .trim();
                if (labelText) return labelText;
              }

              const textContent = String(el.innerText || el.textContent || '').trim();
              if (textContent) return textContent;

              const title = String(el.getAttribute('title') || '').trim();
              if (title) return title;

              const placeholder = String(el.getAttribute('placeholder') || '').trim();
              if (placeholder) return placeholder;

              const tag = String(el.tagName || '').toLowerCase();
              if (tag === 'input') {
                const type = String(el.getAttribute('type') || '').toLowerCase();
                if (['submit', 'button', 'reset'].includes(type)) {
                  const value = String(el.getAttribute('value') || '').trim();
                  if (value) return value;
                }
              }
              return '';
            };

            const unlabeledControls = [];
            for (const el of document.querySelectorAll(interactiveSelector)) {
              if (!isVisible(el) || el.hasAttribute('disabled')) continue;
              const name = accessibleName(el);
              if (!name) {
                unlabeledControls.push(elementSummary(el));
              }
            }

            return {
              mainCount: document.querySelectorAll('main, [role="main"]').length,
              navCount: document.querySelectorAll('nav, [role="navigation"]').length,
              headingCount: document.querySelectorAll('h1, h2, [role="heading"]').length,
              unlabeledControls: unlabeledControls.slice(0, 20),
              unlabeledCount: unlabeledControls.length,
            };
        }"""
    )


class TestAccessibilitySmoke:
    def test_key_pages_expose_accessibility_basics(
        self,
        page,
        server_reachable,
        base_url,
        active_test_role,
    ):
        failures: list[str] = []
        for path in _routes_for_role(active_test_role):
            response = page.goto(f"{base_url}{path}", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("load", timeout=15_000)
            page.wait_for_function(
                """() => {
                    const root = document.getElementById('root');
                    if (!root) return Boolean(document.querySelector('main, nav, h1, form'));
                    if (root.childElementCount > 0) return true;
                    return Boolean(document.querySelector('main, nav, h1, form'));
                }""",
                timeout=15_000,
            )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                failures.append(f"{path}: page returned HTTP {status}")
                continue

            content = page.content()
            if _is_login_page(content):
                path = f"{path} -> /login"

            report = _accessibility_report(page)
            if int(report.get("mainCount") or 0) < 1:
                failures.append(f"{path}: missing <main> landmark")
            if int(report.get("navCount") or 0) < 1:
                failures.append(f"{path}: missing navigation landmark")
            if int(report.get("headingCount") or 0) < 1:
                failures.append(f"{path}: missing primary heading")

            unlabeled_count = int(report.get("unlabeledCount") or 0)
            if unlabeled_count > 0:
                sample = report.get("unlabeledControls") or []
                failures.append(
                    f"{path}: found {unlabeled_count} visible interactive control(s) "
                    f"without accessible name; sample={sample}"
                )

        assert not failures, "Accessibility smoke failures:\n" + "\n".join(failures)
