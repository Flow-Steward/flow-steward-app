"""
UI tests for Dashboard, Jobs, Events, Health, Monitoring, Settings, and Extensions pages.

Tests:
- Projects dashboard: loads, title, create button, sidebar
- Jobs page: loads, filter controls, table/grid, empty state
- Events page: loads, timeline visible
- Health endpoints: /health/live, /health/ready
- Monitoring page: loads, tabs visible
- Settings page: loads without the old Extensions tab
- Extensions page: loads and exposes Installed/Marketplace

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_dashboard_pages.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _is_login_page(content: str) -> bool:
    body = str(content or "").lower()
    return "login" in body and ("password" in body or "sign in" in body or "username" in body)


def _is_spa_shell(content: str) -> bool:
    body = str(content or "").lower()
    return '<div id="root"></div>' in body and "/app/assets/index" in body


class TestProjectsDashboard:
    """Projects dashboard page (/projects)."""

    def test_projects_page_loads(self, page, server_reachable, base_url):
        """Projects page returns 200 and renders content."""
        resp = requests.get(f"{base_url}/projects", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_projects_page_has_title(self, page, server_reachable, base_url):
        """Page title contains 'Projects'."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        title = page.title()
        content = page.content().lower()
        assert "project" in title.lower() or "project" in content

    def test_projects_page_has_create_button(self, page, server_reachable, base_url):
        """A 'New Project' or 'Create' button is visible."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        btn = page.locator(
            "button:has-text('Create'), button:has-text('New'), "
            "a:has-text('Create'), a:has-text('New Project'), "
            "[data-action='create-project']"
        )
        content = page.content().lower()
        assert btn.count() > 0 or "create" in content or "new project" in content

    def test_projects_page_has_sidebar(self, page, server_reachable, base_url):
        """Sidebar navigation is visible."""
        page.goto(f"{base_url}/projects", wait_until="networkidle", timeout=20_000)
        sidebar = page.locator("#sidebar, nav.sidebar, [data-testid='sidebar'], aside")
        assert sidebar.count() > 0


class TestJobsPage:
    """Jobs page (/jobs)."""

    def test_jobs_page_loads(self, page, server_reachable, base_url):
        """Jobs page returns 200."""
        resp = requests.get(f"{base_url}/jobs", verify=False, timeout=10)
        assert resp.status_code == 200

    def test_jobs_page_has_filter_controls(self, page, server_reachable, base_url):
        """Jobs page has status filter or search box."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        filters = page.locator(
            "select, input[type='search'], input[placeholder*='search'], "
            "input[placeholder*='filter'], .filter-controls, [data-testid='filter']"
        )
        content = page.content().lower()
        assert filters.count() > 0 or "filter" in content or "search" in content

    def test_jobs_page_has_table_or_grid(self, page, server_reachable, base_url):
        """Jobs page has a platform-grid, table, or list element."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        grid = page.locator("platform-grid, table, .jobs-list, [data-testid='jobs-grid']")
        assert grid.count() > 0

    def test_jobs_page_empty_state(self, page, server_reachable, base_url):
        """Jobs page shows content even when no jobs exist (empty state or table)."""
        page.goto(f"{base_url}/jobs", wait_until="networkidle", timeout=20_000)
        content = page.content()
        # Page should render something meaningful — either a table or an empty-state message
        assert len(content) > 200


class TestEventsPage:
    """Events tab (/monitoring?tab=events)."""

    def test_events_page_loads(self, page, server_reachable, base_url):
        """Monitoring events tab hydrates real React controls."""
        page.goto(f"{base_url}/monitoring?tab=events", wait_until="networkidle", timeout=20_000)
        if _is_login_page(page.content()):
            pytest.skip("Events tab requires authentication")
        try:
            page.wait_for_selector("#events-grid", timeout=15_000)
        except Exception:
            assert (
                page.get_by_text("Events list").count() > 0
                or page.get_by_text("No events found.").count() > 0
                or page.get_by_text("Failed to load events.").count() > 0
            )
        else:
            assert page.query_selector("#events-grid") is not None

    def test_events_page_has_timeline(self, page, server_reachable, base_url):
        """Monitoring events tab has a timeline or event list element."""
        page.goto(f"{base_url}/monitoring?tab=events", wait_until="networkidle", timeout=20_000)
        timeline = page.locator(
            ".timeline, .event-list, [data-testid='timeline'], table, platform-grid, .events"
        )
        content = page.content().lower()
        is_login = _is_login_page(content)
        if is_login:
            assert "login" in content
        else:
            assert timeline.count() > 0 or "event" in content


class TestMonitoringPage:
    """Monitoring page (/monitoring)."""

    def test_monitoring_page_loads(self, page, server_reachable, base_url):
        """Monitoring page returns content or a clear auth boundary."""
        resp = requests.get(
            f"{base_url}/monitoring", verify=False, timeout=10, allow_redirects=False
        )
        assert resp.status_code in (200, 302, 401)
        if resp.status_code == 302:
            assert "/login" in str(resp.headers.get("location") or "")
        elif resp.status_code == 401:
            assert "authentication required" in resp.text.lower()
        else:
            body = resp.text.lower()
            assert "monitoring" in body or _is_login_page(body) or _is_spa_shell(body)

    def test_monitoring_page_has_tabs(self, page, server_reachable, base_url):
        """Monitoring page exposes monitoring tabs."""
        page.goto(f"{base_url}/monitoring", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        is_login = _is_login_page(content)
        if is_login:
            assert "login" in content
        else:
            tabs = page.locator(
                "[data-section='monitoring'], [data-section='monitoring-overview'], "
                "button:has-text('Overview'), button:has-text('Processes'), button:has-text('Events')"
            )
            assert tabs.count() > 0 or "monitoring" in content


class TestHealthEndpoints:
    """Health check endpoints."""

    def test_health_live_returns_200(self, server_reachable, base_url):
        """/health/live returns 200."""
        resp = requests.get(f"{base_url}/health/live", verify=False, timeout=5)
        assert resp.status_code == 200

    def test_health_ready_endpoint(self, server_reachable, base_url):
        """/health/ready exists and returns 200 or 503."""
        resp = requests.get(f"{base_url}/health/ready", verify=False, timeout=5)
        assert resp.status_code in (200, 503)


class TestShellSearch:
    """Global shell search controls."""

    def test_shell_search_has_category_and_submit_controls(self, page, server_reachable, base_url):
        """Header search includes category selector and explicit Search button."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return
        category = page.locator("#global-search-category, [data-testid='global-search-category']")
        submit = page.locator("#global-search-submit, [data-testid='global-search-submit']")
        assert category.count() > 0
        assert submit.count() > 0

    def test_shell_search_opens_results_panel(self, page, server_reachable, base_url):
        """Submitting global search opens results panel or typed error state."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return
        page.fill("#global-search", "job")
        page.select_option("#global-search-category", "jobs")
        page.wait_for_function(
            """() =>
            Boolean(document.querySelector('[data-testid="global-search-results"]')) ||
            !document.querySelector('[data-testid="global-search-submit"]')?.disabled
            """,
            timeout=20_000,
        )
        if page.locator("[data-testid='global-search-results']").count() == 0:
            page.click("[data-testid='global-search-submit']")
        page.wait_for_function(
            """() =>
            Boolean(document.querySelector('[data-testid="global-search-results"]')) ||
            document.body.innerText.toLowerCase().includes('search request failed')
            """,
            timeout=20_000,
        )
        panel = page.locator("[data-testid='global-search-results']")
        assert panel.count() > 0


class TestSettingsPage:
    """Settings page (/settings)."""

    def test_settings_page_loads(self, page, server_reachable, base_url):
        """Settings page returns content or a clear auth boundary."""
        resp = requests.get(f"{base_url}/settings", verify=False, timeout=10, allow_redirects=False)
        assert resp.status_code in (200, 302, 401)
        if resp.status_code == 302:
            assert "/login" in str(resp.headers.get("location") or "")
        elif resp.status_code == 401:
            assert "authentication required" in resp.text.lower()
        else:
            body = resp.text.lower()
            assert "setting" in body or _is_login_page(body) or _is_spa_shell(body)

    def test_settings_page_does_not_show_extensions_tab(self, page, server_reachable, base_url):
        """Settings should no longer expose Extensions inside the hub tab bar."""
        page.goto(f"{base_url}/settings", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        extensions_tab = page.locator("#settings-tabs [data-tab='extensions']")
        is_login = _is_login_page(content)
        if is_login:
            assert "login" in content
        else:
            assert extensions_tab.count() == 0

    def test_extensions_page_loads(self, page, server_reachable, base_url):
        """/extensions returns content or a clear auth boundary."""
        resp = requests.get(
            f"{base_url}/extensions", verify=False, timeout=10, allow_redirects=False
        )
        assert resp.status_code in (200, 302, 401)
        if resp.status_code == 302:
            assert "/login" in str(resp.headers.get("location") or "")
        elif resp.status_code == 401:
            assert "authentication required" in resp.text.lower()
        else:
            body = resp.text.lower()
            assert ("installed" in body and "marketplace" in body) or _is_spa_shell(body)

    def test_extensions_table_fits_desktop_content_area(self, page, server_reachable, base_url):
        """At 1440px, status and actions remain visible without horizontal recovery."""
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base_url}/extensions", wait_until="networkidle", timeout=20_000)
        if _is_login_page(page):
            pytest.fail("Not authenticated")
        table = page.locator("table").first
        table.wait_for(state="visible", timeout=10_000)
        geometry = table.evaluate(
            """(node) => {
                const container = node.parentElement;
                const row = node.querySelector('tbody tr');
                const status = row?.querySelector('td:nth-child(5)');
                const actions = row?.querySelector('td:nth-child(6)');
                const tableRect = node.getBoundingClientRect();
                const containerRect = container?.getBoundingClientRect();
                const statusRect = status?.getBoundingClientRect();
                const actionsRect = actions?.getBoundingClientRect();
                return {
                    tableRight: tableRect.right,
                    containerRight: containerRect?.right || 0,
                    scrollWidth: container?.scrollWidth || 0,
                    clientWidth: container?.clientWidth || 0,
                    statusWidth: statusRect?.width || 0,
                    actionsWidth: actionsRect?.width || 0,
                    actionsRight: actionsRect?.right || 0,
                };
            }"""
        )
        assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
        assert geometry["tableRight"] <= geometry["containerRight"] + 1, geometry
        assert geometry["actionsRight"] <= geometry["containerRight"] + 1, geometry
        assert geometry["statusWidth"] >= 120, geometry
        assert geometry["actionsWidth"] >= 120, geometry

    def test_settings_model_providers_alias_is_removed(self, server_reachable, base_url):
        """Retired alias /settings/model-providers should no longer resolve to a mapped app surface."""
        resp = requests.get(
            f"{base_url}/settings/model-providers",
            verify=False,
            timeout=10,
            allow_redirects=False,
        )
        assert resp.status_code in (401, 404)

    def test_settings_mcp_connectors_route_is_not_registered(self, server_reachable, base_url):
        """Retired MCP settings routes should no longer resolve to a mapped app surface."""
        resp = requests.get(
            f"{base_url}/settings/mcp-connectors",
            verify=False,
            timeout=10,
            allow_redirects=False,
        )
        assert resp.status_code in (401, 404)

    def test_settings_hub_model_providers_renders_inline_surface(
        self, page, server_reachable, base_url
    ):
        """/settings hub should render provider forms inline instead of fallback Open-link cards."""
        page.goto(f"{base_url}/settings", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return

        model_providers_tab = page.locator(
            "#settings-tabs [data-tab='model_providers'], #settings-tabs [data-tab='providers']"
        ).first
        if model_providers_tab.count() == 0:
            assert "settings" in content
            assert "this settings area is available as a dedicated react screen" not in content
            return
        model_providers_tab.click()

        providers_panel = page.locator("#model_providers-content, #providers-content").first
        providers_panel.wait_for(state="visible", timeout=10_000)
        providers_text = providers_panel.inner_text().lower()
        assert "manage model providers" in providers_text
        assert "add provider" in providers_text
        fallback_hint = page.locator(
            "text=This settings area is available as a dedicated React screen."
        )
        if fallback_hint.count():
            assert not fallback_hint.first.is_visible()

    def test_registry_mcp_page_resolves(self, page, server_reachable, base_url):
        """/registry/mcp should resolve to the canonical MCP catalog surface."""
        page.goto(f"{base_url}/registry/mcp", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return

        assert "this react route is not mapped" not in content
        assert "mcp" in content

    def test_extensions_open_link_opens_detail_surface(self, page, server_reachable, base_url):
        """Installed extension 'Open' action should navigate to detail route and render detail panel."""
        page.goto(f"{base_url}/extensions", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return

        open_link = page.locator("a:has-text('Open')").first
        if open_link.count() == 0:
            resp = requests.get(
                f"{base_url}/api/extensions?state=all",
                verify=False,
                timeout=10,
            )
            assert resp.status_code == 200
            payload = resp.json()
            rows = payload if isinstance(payload, list) else payload.get("extensions", [])
            assert isinstance(rows, list), f"Unexpected extensions payload: {payload!r}"
            if not rows:
                assert (
                    "no extensions" in content
                    or "marketplace is coming soon" in content
                    or "installed" in content
                )
                return
            first_row = rows[0] if isinstance(rows[0], dict) else {}
            extension_id = str(
                first_row.get("extension_id") or first_row.get("id") or first_row.get("key") or ""
            ).strip()
            assert extension_id, f"Could not derive extension id from payload row: {first_row!r}"
            page.goto(
                f"{base_url}/extensions/{extension_id}",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            page.wait_for_function(
                """() => !!document.getElementById('extension-detail-panel')""",
                timeout=10_000,
            )
            assert page.locator("#extension-detail-panel").count() > 0
            return

        with page.expect_navigation(wait_until="commit", timeout=20_000):
            open_link.click()

        page.wait_for_url("**/extensions/**", timeout=10_000)
        assert "/extensions/" in page.url
        page.wait_for_function(
            """() => !!document.getElementById('extension-detail-panel')""",
            timeout=10_000,
        )
        assert page.locator("#extension-detail-panel").count() > 0


class TestShellTheme:
    """Topbar theme toggle contracts."""

    def test_theme_toggle_updates_shell_palette(self, page, server_reachable, base_url):
        """Theme toggle must change both root theme state and visible shell palette."""
        page.goto(f"{base_url}/dashboard", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return

        toggle = page.locator("[data-testid='theme-toggle']").first
        if toggle.count() == 0:
            pytest.skip("Theme toggle is not rendered in this environment")

        before = page.evaluate(
            """() => {
                const html = document.documentElement;
                const shell = document.querySelector('.app-shell-root');
                const style = shell ? getComputedStyle(shell) : null;
                return {
                    theme: String(html.getAttribute('data-theme') || ''),
                    bg: String(style?.backgroundImage || ''),
                    fg: String(style?.color || ''),
                };
            }"""
        )
        toggle.click()
        page.wait_for_timeout(180)
        after = page.evaluate(
            """() => {
                const html = document.documentElement;
                const shell = document.querySelector('.app-shell-root');
                const style = shell ? getComputedStyle(shell) : null;
                return {
                    theme: String(html.getAttribute('data-theme') || ''),
                    bg: String(style?.backgroundImage || ''),
                    fg: String(style?.color || ''),
                };
            }"""
        )

        assert after["theme"] in {"dark", "light"}
        assert after["theme"] != before["theme"]
        assert (after["bg"] != before["bg"]) or (after["fg"] != before["fg"])


class TestMonitoringSurfaceAgreesWithReadinessRS5007:
    """The monitoring page must show the same failing checks /health/ready reports.

    The other monitoring tests here assert only that the page loads and has
    tabs, which a page rendering nothing at all also satisfies. That is how the
    dashboard could report "Failed: 0" on a deployment with two failing checks
    without any test noticing. This one compares the browser surface against
    the readiness truth.
    """

    @staticmethod
    def _readiness(base_url: str) -> dict:
        response = requests.get(f"{base_url}/health/ready", verify=False, timeout=15)
        payload = response.json()
        checks = payload.get("checks")
        failing = (
            sorted(
                str(key)
                for key, value in checks.items()
                if isinstance(value, dict) and value.get("ok") is False
            )
            if isinstance(checks, dict)
            else []
        )
        return {"ready": bool(payload.get("ready")), "failing": failing}

    @staticmethod
    def _page_label(check_id: str) -> str:
        """Mirror the page's labelFromKey: stuck_jobs -> Stuck Jobs."""
        return " ".join(word.capitalize() for word in check_id.replace("-", " ").split("_"))

    def test_services_tab_names_every_failing_readiness_check(
        self, page, server_reachable, base_url
    ):
        """Each check /health/ready fails on must be named on the Services tab."""
        readiness = self._readiness(base_url)
        page.goto(f"{base_url}/monitoring?tab=services", wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3_000)
        content = page.content()
        if _is_login_page(content.lower()):
            pytest.skip("UI lane is unauthenticated; monitoring page redirected to login")

        # Section headings are upper-cased by CSS, so compare case-insensitively
        # against the rendered text rather than the source casing.
        rendered = page.locator("body").inner_text().lower()
        assert "readiness checks" in rendered, (
            "Services tab rendered without its readiness section, so it cannot "
            "be reporting deployment truth"
        )
        if not readiness["failing"]:
            pytest.skip("deployment is ready; nothing to cross-check in this run")

        missing = [
            check_id
            for check_id in readiness["failing"]
            if check_id.lower() not in rendered
            and self._page_label(check_id).lower() not in rendered
        ]
        assert not missing, (
            f"/health/ready fails on {readiness['failing']} "
            f"but the Services tab never names {missing}"
        )

    def test_monitoring_page_counts_match_readiness(self, page, server_reachable, base_url):
        """A not-ready deployment must not read as ready, or as zero failures.

        The header used to render the zero-defaults of a payload the page had
        failed to load, so "Failed: 0" appeared while checks were failing.
        """
        readiness = self._readiness(base_url)
        page.goto(f"{base_url}/monitoring", wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3_000)
        if _is_login_page(page.content().lower()):
            pytest.skip("UI lane is unauthenticated; monitoring page redirected to login")
        header = page.locator("body").inner_text()

        expected_ready = "Ready: yes" if readiness["ready"] else "Ready: no"
        assert expected_ready in header, (
            f"readiness reports ready={readiness['ready']} but the page header does not"
        )
        if readiness["failing"]:
            assert f"Failed checks: {len(readiness['failing'])}" in header, (
                f"{len(readiness['failing'])} checks are failing but the page header "
                f"does not report that count"
            )
            assert "Failed checks: 0" not in header, "page reports zero failures while checks fail"
