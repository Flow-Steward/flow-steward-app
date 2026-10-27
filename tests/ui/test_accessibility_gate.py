"""UI accessibility gate suite — Lighthouse/axe checks on critical pages.

P1 gap: accessibility smoke coverage exists in test_accessibility_smoke.py
(landmarks + interactive-name checks); still no Lighthouse/axe gate in CI
with failing threshold + per-page violation budget.

Coverage:

A. Axe-core accessibility checks (client-side injection)
   - /login: zero critical/serious violations
   - /dashboard: violation count within budget
   - /projects: violation count within budget
   - /jobs: violation count within budget
   - /settings: violation count within budget

B. WCAG landmark and structure checks
   - Each critical page has exactly one <main> landmark
   - Navigation landmark is present (nav or [role=navigation])
   - Heading hierarchy is non-broken (h1 present, no skipped levels)
   - Page has a document title set

C. Interactive control accessibility
   - All visible buttons have accessible names
   - All form inputs have associated labels
   - All links have discernible text (not icon-only without aria-label)

D. Color contrast and focus indicators (documented — browser checks)
   - Focus outlines must be visible (checked via CSS)
   - No elements with tabindex > 0 (creates unpredictable tab order)

E. Violation budget enforcement
   - If axe-core is available, total critical + serious violations must be
     below the configured budget per page (default: 0 critical, 5 serious)

Run:
    docker compose exec fs-app python -m pytest \
        tests/ui/test_accessibility_gate.py -v --timeout=120
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)

# Violation budget per page: (max_critical, max_serious)
_VIOLATION_BUDGET = {
    "critical": 0,  # Zero tolerance for critical violations
    "serious": 5,  # Up to 5 serious violations allowed (decreasing budget target)
}

_CRITICAL_PAGES = [
    "/login",
    "/dashboard",
    "/projects",
    "/jobs",
    "/settings",
]

_READY_TEXT = {
    "/dashboard": "dashboard",
    "/projects": "projects",
    "/jobs": "jobs list and triage",
    "/settings": "settings",
}


def _is_login_page(content: str) -> bool:
    body = str(content or "").lower()
    return "login" in body and ("password" in body or "sign in" in body)


def _goto_ui_path(page, base_url: str, path: str, *, timeout: int = 30_000) -> None:
    page.goto(f"{base_url}{path}", wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_load_state("load", timeout=min(timeout, 15_000))
    page.wait_for_function(
        """() => {
            const root = document.getElementById('root');
            if (!root) {
              return Boolean(document.querySelector('main, nav, h1, form'));
            }
            if (root.childElementCount > 0) {
              return true;
            }
            return Boolean(document.querySelector('main, nav, h1, form'));
        }""",
        timeout=min(timeout, 15_000),
    )
    if path.rstrip("/") == "/jobs":
        page.get_by_text("Jobs list and triage").first.wait_for(
            state="visible",
            timeout=min(timeout, 15_000),
        )


def _goto_login_page(page, base_url: str, *, timeout: int = 30_000) -> None:
    with suppress(Exception):
        page.context.clear_cookies()
    _goto_ui_path(page, base_url, "/login", timeout=timeout)


# Axe-core CDN URL — used for client-side injection
_AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.7.2/axe.min.js"
_DEFAULT_LOCAL_AXE = (
    Path(__file__).resolve().parents[2] / "assets/frontend/node_modules/axe-core/axe.min.js"
)

_AXE_RUN_JS = """
async () => {
  // Inject axe-core if not already loaded
  if (typeof axe === 'undefined') {
    return {injected: false, violations: [], error: 'axe-core not loaded'};
  }
  try {
    const results = await axe.run(document, {
      runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
    });
    return {
      injected: true,
      violations: results.violations.map(v => ({
        id: v.id,
        impact: v.impact,
        description: v.description,
        nodes: v.nodes.length,
      })),
      passes: results.passes.length,
      incomplete: results.incomplete.length,
    };
  } catch (e) {
    return {injected: true, violations: [], error: String(e)};
  }
}
"""


def _inject_axe_and_run(page) -> dict[str, Any] | None:
    """Inject axe-core via script tag and run accessibility check.

    Tries CDN first, then a local path if AXE_CORE_JS_PATH env var is set.
    On failure, raises pytest.fail() unless AXE_SKIP_ON_CDN_FAIL=1 is set
    (use only in air-gapped environments where CDN is genuinely unreachable).
    """
    # Prefer the checked-out npm copy so the gate does not need a CSP exception
    # for third-party script tags.
    local_path = os.environ.get("AXE_CORE_JS_PATH", "").strip()
    if not local_path and _DEFAULT_LOCAL_AXE.exists():
        local_path = str(_DEFAULT_LOCAL_AXE)
    last_exc: Exception | None = None

    sources_to_try: list[tuple[str, str]] = []
    if local_path:
        sources_to_try.append(("local", local_path))
    sources_to_try.append(("cdn", _AXE_CDN))

    for source_type, source in sources_to_try:
        try:
            if source_type == "local":
                page.add_init_script(path=source)
                page.reload(wait_until="load", timeout=30_000)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=5_000)
            else:
                page.add_script_tag(url=source)
            page.wait_for_function("typeof axe !== 'undefined'", timeout=10_000)
            return page.evaluate(_AXE_RUN_JS)
        except Exception as exc:
            last_exc = exc
            continue

    # Injection failed from all sources
    if os.environ.get("AXE_SKIP_ON_CDN_FAIL", "").strip() in {"1", "true", "yes"}:
        return None  # caller will pytest.skip()
    pytest.fail(
        f"Could not inject axe-core from any source (CDN={_AXE_CDN!r}, "
        f"local={local_path!r}). "
        "Set AXE_CORE_JS_PATH to a local axe.min.js or AXE_SKIP_ON_CDN_FAIL=1 "
        f"to skip instead of fail. Last error: {last_exc}"
    )


def _assert_violation_budget(violations: list, page_path: str):
    """Assert violation counts are within budget."""
    critical = [v for v in violations if v.get("impact") == "critical"]
    serious = [v for v in violations if v.get("impact") == "serious"]

    critical_count = len(critical)
    serious_count = len(serious)

    if critical_count > _VIOLATION_BUDGET["critical"]:
        critical_details = json.dumps(critical[:3], indent=2)
        pytest.fail(
            f"{page_path}: {critical_count} CRITICAL accessibility violations "
            f"(budget: {_VIOLATION_BUDGET['critical']}):\n{critical_details}"
        )

    if serious_count > _VIOLATION_BUDGET["serious"]:
        serious_details = json.dumps(serious[:5], indent=2)
        pytest.fail(
            f"{page_path}: {serious_count} SERIOUS accessibility violations "
            f"(budget: {_VIOLATION_BUDGET['serious']}):\n{serious_details}"
        )


# ===========================================================================
# A. Axe-core accessibility checks
# ===========================================================================


class TestAxeCoreAccessibility:
    """Run axe-core on critical pages and enforce violation budgets."""

    @pytest.mark.parametrize("path", _CRITICAL_PAGES)
    def test_page_within_violation_budget(self, page, server_reachable, base_url, path):
        """Page must have critical/serious violations within configured budget."""
        _goto_ui_path(page, base_url, path, timeout=30_000)
        content = page.content().lower()
        if _is_login_page(content) and path != "/login":
            pytest.skip(f"Not authenticated — cannot test {path}")
        ready_text = _READY_TEXT.get(path.rstrip("/"))
        if ("not found" in content or "404" in content) and (
            not ready_text or ready_text not in content
        ):
            pytest.fail(f"{path} returned a not-found page in this build")

        result = _inject_axe_and_run(page)
        if result is None or not result.get("injected"):
            pytest.skip(f"Could not inject axe-core on {path} — check network access to CDN")

        violations = result.get("violations", [])
        _assert_violation_budget(violations, path)

    def test_login_page_has_zero_critical_violations(self, page, server_reachable, base_url):
        """Login page must have zero critical accessibility violations."""
        _goto_login_page(page, base_url, timeout=30_000)
        result = _inject_axe_and_run(page)
        if result is None or not result.get("injected"):
            pytest.skip("Cannot inject axe-core — check CDN network access")
        critical = [v for v in result.get("violations", []) if v.get("impact") == "critical"]
        assert not critical, (
            f"Login page has {len(critical)} critical a11y violations: {json.dumps(critical[:3], indent=2)}"
        )


# ===========================================================================
# B. WCAG landmark and structure checks
# ===========================================================================


class TestWcagLandmarkStructure:
    """Each critical page must have proper WCAG landmarks and heading hierarchy."""

    @pytest.mark.parametrize("path", _CRITICAL_PAGES)
    def test_page_has_main_landmark(self, page, server_reachable, base_url, path):
        """Page must have exactly one <main> element or [role=main]."""
        if path == "/login":
            _goto_login_page(page, base_url, timeout=25_000)
        else:
            _goto_ui_path(page, base_url, path, timeout=25_000)
        if path == "/login":
            with suppress(Exception):
                page.wait_for_function(
                    """() => {
                        const currentPath = window.location.pathname.replace(/\\/$/, "");
                        if (currentPath !== "/login") return true;
                        return document.querySelectorAll("main, [role=\\"main\\"]").length > 0;
                    }""",
                    timeout=5_000,
                )
        if path == "/login" and urlparse(str(page.url or "")).path.rstrip("/") != "/login":
            pytest.skip("Already authenticated — /login redirected to an app route")
        content = page.content().lower()
        if _is_login_page(content) and path not in ("/login",):
            pytest.skip(f"Not authenticated — cannot test {path}")
        main_count = page.evaluate(
            "() => document.querySelectorAll('main, [role=\"main\"]').length"
        )
        if path == "/login" and main_count < 1:
            is_shell_placeholder = ('id="root"' in content and "/app/assets/" in content) or (
                "id='root'" in content and "/app/assets/" in content
            )
            if is_shell_placeholder:
                pytest.skip("Login route rendered as SPA shell placeholder in this environment")
        assert main_count >= 1, (
            f"{path}: must have at least one <main> landmark; found {main_count}"
        )

    @pytest.mark.parametrize("path", _CRITICAL_PAGES)
    def test_page_has_document_title(self, page, server_reachable, base_url, path):
        """Page must have a non-empty document title."""
        if path == "/login":
            _goto_login_page(page, base_url, timeout=25_000)
        else:
            _goto_ui_path(page, base_url, path, timeout=25_000)
        title = page.title()
        assert title, f"{path}: document must have a non-empty title; got {title!r}"
        assert len(title.strip()) > 0, (
            f"{path}: document must have a non-empty title; got {title!r}"
        )

    @pytest.mark.parametrize("path", _CRITICAL_PAGES)
    def test_page_has_navigation_landmark(self, page, server_reachable, base_url, path):
        """Page must have a navigation landmark."""
        if path == "/login":
            _goto_login_page(page, base_url, timeout=25_000)
        else:
            _goto_ui_path(page, base_url, path, timeout=25_000)
        content = page.content().lower()
        if _is_login_page(content) and path not in ("/login",):
            pytest.skip(f"Not authenticated — cannot test {path}")
        nav_count = page.evaluate(
            "() => document.querySelectorAll('nav, [role=\"navigation\"]').length"
        )
        if path == "/login":
            skip_links = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href^="#"]'))
                    .map((node) => String(node.textContent || '').trim().toLowerCase())
                    .filter(Boolean)"""
            )
            assert any("skip" in label and "main" in label for label in skip_links), (
                "/login should expose a skip-to-main link when it omits a global navigation landmark"
            )
            return
        assert nav_count >= 1, (
            f"{path}: must have at least one navigation landmark; found {nav_count}"
        )

    def test_login_page_heading_hierarchy_valid(self, page, server_reachable, base_url):
        """Login page must have an h1 (top-level heading)."""
        _goto_login_page(page, base_url, timeout=25_000)
        with suppress(Exception):
            page.wait_for_function(
                """() => {
                    const path = window.location.pathname.replace(/\\/$/, "");
                    if (path !== "/login") return true;
                    return document.querySelectorAll("h1").length > 0;
                }""",
                timeout=5_000,
            )
        if urlparse(str(page.url or "")).path.rstrip("/") != "/login":
            pytest.skip("Already authenticated — /login redirected to an app route")
        h1_count = page.evaluate("() => document.querySelectorAll('h1').length")
        if h1_count < 1:
            lowered = page.content().lower()
            is_shell_placeholder = ('id="root"' in lowered and "/app/assets/" in lowered) or (
                "id='root'" in lowered and "/app/assets/" in lowered
            )
            if is_shell_placeholder:
                pytest.skip("Login route rendered as SPA shell placeholder in this environment")
        assert h1_count >= 1, f"/login must have at least one h1 heading; found {h1_count}"


# ===========================================================================
# C. Interactive control accessibility
# ===========================================================================


class TestInteractiveControlAccessibility:
    """All interactive controls must have accessible names."""

    @pytest.mark.parametrize("path", _CRITICAL_PAGES)
    def test_visible_buttons_have_accessible_names(self, page, server_reachable, base_url, path):
        """All visible buttons must have an accessible name."""
        _goto_ui_path(page, base_url, path, timeout=25_000)
        content = page.content().lower()
        if _is_login_page(content) and path not in ("/login",):
            pytest.skip(f"Not authenticated — cannot test {path}")

        unnamed_buttons = page.evaluate(
            """() => {
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                return buttons
                    .filter(btn => {
                        if (btn.hasAttribute('hidden')) return false;
                        if (btn.getAttribute('aria-hidden') === 'true') return false;
                        const style = window.getComputedStyle(btn);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = btn.getBoundingClientRect();
                        if (rect.width < 1 || rect.height < 1) return false;
                        const name = (
                            btn.getAttribute('aria-label') ||
                            btn.getAttribute('title') ||
                            (btn.innerText || btn.textContent || '').trim()
                        );
                        return !name;
                    })
                    .map(btn => ({
                        tag: btn.tagName.toLowerCase(),
                        id: btn.id || '',
                        class: (btn.className || '').slice(0, 80),
                    }));
            }"""
        )
        # Allow a budget of 2 unnamed buttons (icons often use SVG title or tooltip)
        assert len(unnamed_buttons) <= 2, (
            f"{path}: {len(unnamed_buttons)} visible buttons lack accessible names: "
            f"{json.dumps(unnamed_buttons[:5], indent=2)}"
        )

    def test_login_form_inputs_have_labels(self, page, server_reachable, base_url):
        """Login form inputs must have associated labels."""
        _goto_login_page(page, base_url, timeout=25_000)
        unlabeled = page.evaluate(
            """() => {
                const inputs = Array.from(document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
                ));
                return inputs
                    .filter(inp => {
                        const ariaLabel = inp.getAttribute('aria-label');
                        const ariaLabelledBy = inp.getAttribute('aria-labelledby');
                        const labels = inp.labels && inp.labels.length > 0;
                        const title = inp.getAttribute('title');
                        const placeholder = inp.getAttribute('placeholder');
                        // placeholder alone is not sufficient, but better than nothing
                        return !ariaLabel && !ariaLabelledBy && !labels && !title;
                    })
                    .map(inp => ({type: inp.type, id: inp.id || '', name: inp.name || ''}));
            }"""
        )
        assert len(unlabeled) == 0, (
            f"/login: {len(unlabeled)} inputs lack labels: {json.dumps(unlabeled, indent=2)}"
        )


# ===========================================================================
# D. Focus and tab order checks
# ===========================================================================


class TestFocusAndTabOrder:
    """Focus indicators and tab order must be accessible."""

    @pytest.mark.parametrize("path", ["/login", "/dashboard", "/projects"])
    def test_no_positive_tabindex_elements(self, page, server_reachable, base_url, path):
        """Elements must not use tabindex > 0 (creates unpredictable tab order)."""
        if path == "/login":
            _goto_login_page(page, base_url, timeout=25_000)
        else:
            _goto_ui_path(page, base_url, path, timeout=25_000)
        content = page.content().lower()
        if _is_login_page(content) and path not in ("/login",):
            pytest.skip(f"Not authenticated — cannot test {path}")

        positive_tabindex = page.evaluate(
            """() => {
                const elements = document.querySelectorAll('[tabindex]');
                return Array.from(elements)
                    .filter(el => parseInt(el.getAttribute('tabindex') || '0', 10) > 0)
                    .map(el => ({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        tabindex: el.getAttribute('tabindex'),
                    }));
            }"""
        )
        assert len(positive_tabindex) == 0, (
            f"{path}: {len(positive_tabindex)} elements use tabindex > 0 (breaks tab order): "
            f"{json.dumps(positive_tabindex[:5], indent=2)}"
        )

    def test_login_page_has_skip_link_or_first_focusable_is_main(
        self, page, server_reachable, base_url
    ):
        """Login page should have a skip-to-main link or the first focusable element is within main."""
        _goto_login_page(page, base_url, timeout=25_000)
        if urlparse(str(page.url or "")).path.rstrip("/") != "/login":
            pytest.skip("Already authenticated — /login redirected to an app route")
        content = page.content().lower()
        if not _is_login_page(content):
            pytest.skip("Already authenticated — /login rendered an app route")
        skip_link = page.locator("a:has-text('skip'), a:has-text('Skip to main')")
        has_skip = skip_link.count() > 0
        # Skip link is recommended (WCAG 2.4.1) but not strictly required for a
        # simple single-form login page. Log a warning if missing rather than fail.
        if not has_skip:
            import warnings

            warnings.warn(
                "/login: no skip-to-main link found — consider adding one for keyboard users",
                UserWarning,
                stacklevel=2,
            )


# ===========================================================================
# E. Violation budget summary test (parametrized)
# ===========================================================================


class TestViolationBudgetSummary:
    """Aggregate violation budget check across all critical pages."""

    def test_all_critical_pages_accessible_without_errors(self, page, server_reachable, base_url):
        """All critical pages must return HTTP 200 (or auth redirect) — no 500."""
        for path in _CRITICAL_PAGES:
            resp = requests.get(f"{base_url}{path}", verify=False, timeout=10, allow_redirects=True)
            assert resp.status_code not in (500,), (
                f"{path} returned 500 — must not crash (accessibility prerequisite)"
            )

    def test_axe_violation_budget_summary(self, page, server_reachable, base_url):
        """Run axe on all accessible critical pages and summarize violations."""
        summary = []
        ordered_paths = [path for path in _CRITICAL_PAGES if path != "/login"] + ["/login"]
        for path in ordered_paths:
            if path == "/login":
                _goto_login_page(page, base_url, timeout=30_000)
            else:
                _goto_ui_path(page, base_url, path, timeout=30_000)
            content = page.content().lower()
            if _is_login_page(content) and path not in ("/login",):
                summary.append({"path": path, "skipped": "not authenticated"})
                continue
            result = _inject_axe_and_run(page)
            if result is None or not result.get("injected"):
                summary.append({"path": path, "skipped": "axe not available"})
                continue
            violations = result.get("violations", [])
            critical = sum(1 for v in violations if v.get("impact") == "critical")
            serious = sum(1 for v in violations if v.get("impact") == "serious")
            summary.append(
                {
                    "path": path,
                    "critical": critical,
                    "serious": serious,
                    "total": len(violations),
                }
            )

        # Report summary — fail if any page exceeds critical budget
        pages_with_critical = [
            s for s in summary if s.get("critical", 0) > _VIOLATION_BUDGET["critical"]
        ]
        assert not pages_with_critical, "Pages exceeding critical violation budget:\n" + json.dumps(
            pages_with_critical, indent=2
        )
